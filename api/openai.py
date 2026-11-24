# -*- coding: utf-8 -*-
"""对外 OpenAI 兼容层：/v1/models、/v1/chat/completions。"""
import json
import socket
import sys
import time
import urllib.error
import uuid

from cap import chat as chat_cap
from cap import guard as guard_cap
from core.store import iso, now

PASS_KEYS = chat_cap.PASS_KEYS + ("session_id",)

# 网络瞬断（不是上游业务错误）：本机到 yybadaccess.3g.qq.com 的 TCP 建连被拒/超时。
# 实测 2026-11-21 06:30 与 07:04 两段各持续 3~4 分钟，期间所有请求都是
# WinError 10061（目标计算机积极拒绝），网络恢复后立刻正常。
# 这类错误跟账号无关，不能换号、不能罚号，只能原地退避重试。
NET_ERRNOS = (10061, 10060, 10051, 10065, 10054, 10053)
NET_RETRY_MAX = 4          # 首次 + 3 次重试
NET_RETRY_DELAYS = (1.0, 2.0, 4.0)


def _is_net_error(exc):
    """判断异常是不是「连不上上游」的网络层错误（可重试）。"""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, BaseException):
            return _is_net_error(reason)
        return True
    if isinstance(exc, (ConnectionError, socket.timeout, TimeoutError)):
        return True
    return getattr(exc, "errno", None) in NET_ERRNOS


class ApiError(Exception):
    def __init__(self, status, message, code="", type_="invalid_request_error", param=None):
        Exception.__init__(self, message)
        self.status = status
        self.message = message
        self.code = code or type_
        self.type = type_
        self.param = param

    def payload(self):
        return {"error": {"message": self.message, "type": self.type,
                          "param": self.param, "code": self.code}}


# ---------------- 模型 ----------------
def resolve_slot(models, name):
    """把客户端传来的 model 解析成 (上游槽位, 回显名)。

    认三种写法：alias 主名 / alias 的友好别名 alts / 槽位名直传。
    回显名一律用**客户端传进来的那个字符串**，这样用友好名请求时响应体 model 也是友好名。
    """
    name = (name or "").strip()
    if not name:
        raise ApiError(400, "缺少 model 参数", "missing_model")
    low = name.lower()
    for a in models.get("aliases") or []:
        if a["name"] == name:
            return a["slot"], name
        for alt in a.get("alts") or []:
            if alt.lower() == low:          # 友好别名（大小写不敏感）
                return a["slot"], name
        if a["slot"] == name:              # 槽位名直传放行
            return a["slot"], name
    # slot_budgets 里登记过的槽位也认（有些槽位不一定挂 alias，但预算表里有它）
    if name in (models.get("slot_budgets") or {}):
        return name, name
    known = ", ".join([a["name"] for a in models.get("aliases") or []])
    raise ApiError(404, "模型不存在：%s（可用：%s）" % (name, known), "model_not_found")


def models_payload(models):
    data = []
    for a in models.get("aliases") or []:
        caps = a.get("caps") or {"text": True, "image": False, "tools": True, "reasoning": True}
        item = {"id": a["name"], "object": "model", "created": 0,
                "owned_by": "marvis", "marvis_slot": a["slot"],
                "description": a.get("desc", "")}
        # 能力字段没有 OpenAI 标准，几个客户端各认一套，能给的都给上（多的字段会被忽略）
        item["type"] = "vision" if caps.get("image") else "text"
        item["capabilities"] = {"text": bool(caps.get("text", True)),
                                "vision": bool(caps.get("image")),
                                "image": bool(caps.get("image")),
                                "tools": bool(caps.get("tools")),
                                "reasoning": bool(caps.get("reasoning"))}
        item["modalities"] = ["text", "image"] if caps.get("image") else ["text"]
        # 上下文容量：各客户端读的字段名不一样，主流三种都给（DSH 的模型发现依次认
        # contextWindow / context_window / context_length / max_input_tokens / limit.context）
        if a.get("display"):
            item["name"] = a["display"]
            item["display_name"] = a["display"]      # 有的客户端认这个
        if a.get("alts"):
            item["alt_ids"] = list(a["alts"])        # 友好别名，可直接当 model 传
        cw = a.get("context_window")
        if isinstance(cw, int) and cw > 0:
            item["context_window"] = cw
            item["context_length"] = cw
            item["max_input_tokens"] = cw
            item["contextWindow"] = cw
            item["limit"] = {"context": cw}
        mt = a.get("max_output_tokens")
        if isinstance(mt, int) and mt > 0:
            item["max_output_tokens"] = mt
            item["maxTokens"] = mt
            item["limit"] = dict(item.get("limit") or {}, output=mt)
        data.append(item)
    return {"object": "list", "data": data}


# ---------------- 续期 ----------------
def ensure_token(ctx, acc):
    """401 后单飞续期。成功返回新的 accounts 行，失败返回 None。"""
    from cap import login as login_cap
    pool, store, config = ctx["pool"], ctx["store"], ctx["config"]
    if not acc["refresh_token"]:
        return None
    if not pool.lock_refresh(acc["openid"]):
        return None                      # 别人正在续，让调用方按 401 处理（避免互踢）
    try:
        r = login_cap.refresh_token({"openid": acc["openid"], "access_token": acc["access_token"],
                                     "refresh_token": acc["refresh_token"],
                                     "login_type": acc["login_type"], "guid": acc["guid"]},
                                    timeout=min(30, config.get("http_timeout") or 180))
        if not r.get("ok"):
            sys.stderr.write("[refresh] 账号 %s 续期失败：%s (status=%s code=%s msg=%s) → 标记 expired\n"
                             % (acc["id"], r.get("error"), r.get("status"), r.get("code"), r.get("msg")))
            sys.stderr.flush()
            # 续期救不回来：如实标记，别让 /health 继续报 active 骗人
            try:
                pool.set_status(acc["id"], "expired",
                                note="续期失败（%s），需重新加号：POST /admin/login/start" % (r.get("code") or r.get("error")))
            except Exception:
                pass
            return None
        pool.save_refreshed(acc["id"], r["info"])
        return pool.get(acc["id"])
    finally:
        pool.unlock_refresh(acc["openid"])


def _code_of(raw):
    try:
        obj = json.loads((raw or b"").decode("utf-8", "replace"))
        return str(obj.get("code") or "")
    except Exception:
        return ""


def _classify(ctx, acc, status, raw):
    """把上游错误映射成对外错误，并按规则给账号打分。"""
    code = msg = ""
    try:
        obj = json.loads((raw or b"").decode("utf-8", "replace"))
        code, msg = obj.get("code"), obj.get("msg") or obj.get("message")
    except Exception:
        msg = (raw or b"")[:200].decode("utf-8", "replace")
    if status != 200:
        sys.stderr.write("[upstream] status=%s code=%s msg=%s\n" % (status, code, (msg or "")[:160]))
        sys.stderr.flush()
    if status == 401:
        api = ApiError(401, msg or "登录态过期", code or "unauthorized", "authentication_error")
    elif status == 429:
        api = ApiError(429, msg or "上游限流", code or "rate_limited", "rate_limit_error")
    elif status >= 500:
        api = ApiError(502, msg or "上游错误 %s" % status, code or "upstream_error", "server_error")
    elif status == 400 and str(code) == "4130001":
        api = ApiError(400, msg or "无效模型 id", "model_not_found")
    else:
        api = ApiError(status if 400 <= status < 500 else 502, msg or "上游错误", code or "upstream_error")
    return api, code, msg


def _marvis_meta(hdrs, slot, acc, ms):
    return {
        "slot": slot,
        "account": (acc["openid"] or "")[:6] + "…",
        "actual_model": (hdrs.get("Marvis-Actual-Model") if hdrs else "") or "",
        "routed_model": (hdrs.get("Marvis-Routed-Model") if hdrs else "") or "",
        "experiment": (hdrs.get("Marvis-Experiment-Id") if hdrs else "") or "",
        "trace": (hdrs.get("X-Trace-Id") if hdrs else "") or "",
        "ms": ms,
    }


# ---------------- 预算决策 ----------------
def _has_image(messages):
    for m in messages or []:
        c = m.get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "image_url":
                    return True
    return False


def _model_budget(models, slot):
    """该槽位的推理预算下限（来自 models.json 的 reasoning_budget）。"""
    best = None
    for a in models.get("aliases") or []:
        if a.get("slot") == slot and a.get("reasoning_budget") is not None:
            best = int(a["reasoning_budget"])
            if best:
                return best
    if best is not None:
        return best
    sb = models.get("slot_budgets") or {}
    if slot in sb:
        return int(sb[slot])
    return int(models.get("reasoning_budget_default") or 0)


def _model_caps(models, slot):
    """该槽位的能力表（models.json 的 caps）。找不到时返回空 dict，调用方按「支持」处理。"""
    for a in models.get("aliases") or []:
        if a.get("slot") == slot and isinstance(a.get("caps"), dict):
            return a["caps"]
    return {}


EFFORT_LEVELS = ("none", "low", "medium", "high", "max")


def _normalize_effort(config, raw):
    """把客户端给的思考档位收成上游认的五个值之一。

    2026-11-24 实测 deepseek-v4-1-flash-261017（其余 flash 同）：
      · 不带 reasoning_effort     → 上游照样推理，但把整段推理写进 delta.content
      · reasoning_effort=none     → 同上（696 字全混进正文）
      · reasoning_effort=off      → 上游直接 502 invalid parameter（它不认 off）
      · low / medium / high / max → delta.reasoning_content 正常分离

    也就是说「关掉思考」这件事上游做不到：关只会让它从后门（content）溜出来，
    客户端只能当正文渲染，用户就看到一大段「让我先一步一步来…」。
    所以 off / none / 空 一律抬到 reasoning_effort_floor（默认 low）：
    对客户端是「尽量少想」，对上游是「至少给个档位，把思考分到 reasoning 通道」。
    """
    floor = str(config.get("reasoning_effort_floor") or "low").strip().lower()
    if floor not in EFFORT_LEVELS or floor == "none":
        floor = "low"
    if raw is None:
        return floor
    v = str(raw).strip().lower()
    if not v or v in ("off", "none", "false", "0", "no", "disabled"):
        return floor
    if v in EFFORT_LEVELS:
        return v
    if v in ("xhigh", "x-high", "highest", "ultra", "maximum"):
        return "max"
    if v in ("minimal", "minimum", "min", "tiny", "least"):
        return "low"
    return floor


def _decide_budget(config, models, slot, messages, body, kw):
    """返回 (目标 max_tokens, 是否调整过, 原因)。三层：分档下限 / 事前抬预算 / 客户端未给"""
    cur = int(kw.get("max_tokens") or kw.get("max_completion_tokens") or 0)
    floor = max(int(config.get("reasoning_floor_tokens") or 0), _model_budget(models, slot))
    preempt = int(config.get("preempt_min_tokens") or 0)
    heavy = _has_image(messages) or bool(body.get("tools"))
    target, why = cur, ""
    if floor > 0 and (not cur or cur < floor):
        target, why = floor, "floor"
    if preempt > 0 and heavy and (not target or target < preempt):
        target, why = preempt, "preempt"
    return target, (target != cur), why


def _should_retry(ctx, raw, hdrs, cur_budget):
    """L3 兜底：正文被 reasoning 吃光时，按实测推理量重算预算。"""
    text = (raw or b"").decode("utf-8", "replace")
    try:
        obj = aggregate_sse(text) if chat_cap.is_sse(hdrs, raw or b"") else json.loads(text)
    except Exception:
        return False, 0
    ch = (obj.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    if ch.get("finish_reason") != "length":
        return False, 0
    if len(msg.get("content") or "") >= 8:
        return False, 0
    reasoning = msg.get("reasoning_content") or ""
    if not reasoning:
        return False, 0
    usage = obj.get("usage") or {}
    rt_hint = int(((usage.get("completion_tokens_details") or {}).get("reasoning_tokens")) or 0)
    # 上游的 reasoning_tokens 有时低于实际（实测正文 886 字只报 256），
    # 所以取「上游数字」和「按中文字符/2 估算」的较大者
    rt = max(rt_hint, max(1, len(reasoning) // 2))
    cap = int(ctx["config"].get("reasoning_retry_cap") or 4096)
    # 实测：同一问题 max_tokens=768 正文仍空，1024 才出正文，2048/4096 更稳。
    # 所以重试预算 = max(1024, 2×已用推理 + 256)，向上取整到 256 的倍数。
    need = max(1024, 2 * rt + 256)
    need = ((need + 255) // 256) * 256
    need = min(cap, need)
    if need <= int(cur_budget or 0):
        return False, 0
    return True, need


# ---------------- 聊天 ----------------
def chat(ctx, body, client_key):
    pool, config, models = ctx["pool"], ctx["config"], ctx["models"]
    slot, name = resolve_slot(models, body.get("model"))
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ApiError(400, "messages 必须是非空数组", "invalid_messages", param="messages")
    want_stream = bool(body.get("stream"))
    session_id = body.get("user") or body.get("session_id") or ""
    kw = dict((k, body.get(k)) for k in PASS_KEYS if k in body and k != "session_id")
    # 思考档位收口：不传、传 none、传 off 都会让上游把整段推理写进正文（详见 _normalize_effort）。
    # 只对支持推理的槽位做 —— hy3 / 视觉档传了会被上游当非法参数。
    if _model_caps(models, slot).get("reasoning", True):
        _raw_effort = body.get("reasoning_effort")
        if _raw_effort is None:
            _raw_effort = body.get("reasoningEffort")   # 有的客户端用驼峰
        kw["reasoning_effort"] = _normalize_effort(config, _raw_effort)
    else:
        kw.pop("reasoning_effort", None)
    # 上游只认 max_tokens；OpenAI 新字段 max_completion_tokens 直接传会被 400（code 3000101）
    if kw.get("max_completion_tokens") is not None and not kw.get("max_tokens"):
        kw["max_tokens"] = kw.pop("max_completion_tokens")
    if want_stream:
        kw.setdefault("stream_options", None)
        kw.pop("stream_options", None)

    acc = pool.pick(client_key, session_id, respect_limit=True)
    if acc is None:
        # 区分「没有号」和「号都在忙」：前者要用户去加号，后者让客户端退避重试
        if pool.counts().get("active"):
            raise ApiError(429, "所有账号并发已满（account_max_concurrency），请稍后重试",
                           "account_busy", "rate_limit_error")
        raise ApiError(503, "账号池里没有可用账号，请先在 /admin/login/start 加号", "no_account", "service_unavailable")
    pool.acquire(acc["id"])

    # 预算决策：推理模型把 max_tokens 当「思考+正文」总预算，太小时正文一个字都轮不到。
    # 三层 = 分档下限(models.json) / 带图带 tools 事前抬 / 客户端没给时兜底。
    target, raised, why = _decide_budget(config, models, slot, messages, body, kw)
    if target:
        kw["max_tokens"] = target
    retried = None

    # 护栏：同一段 system 上次用哪个等级过的，这次直接用（L1 策略记忆）
    grd_fp = guard_cap.fingerprint(messages)
    grd_level = 0 if guard_cap.breaker_open() else guard_cap.recall(ctx, grd_fp)
    if grd_level:
        sys.stderr.write("[guard] 命中策略记忆 fp=%s level=%d\n" % (grd_fp, grd_level))
        sys.stderr.flush()
    payload = chat_cap.body(slot, messages, want_stream, level=grd_level, ctx=ctx, **kw)
    # dev_ext: None=跟配置；True=这次强制补 Ual-Access-MarvisExt（风控 4100404 的自愈路径）
    dev_ext = None
    # 循环里处理四类重试：401 续期、403 护栏升级、403 风控补设备头、预算不足重算
    for attempt in range(1, 7):
        try:
            if want_stream:
                resp = chat_cap.stream(acc, config, payload, ext=dev_ext,
                                       meta=chat_cap.request_meta(slot))
                st = getattr(resp, "status", 200)
                if st != 200:
                    # 上游在建立流之前就拒了（403/401/429/5xx）：读原文按状态码分类
                    try:
                        raw_err = resp.read()
                    except Exception:
                        raw_err = b""
                    resp.close()
                    if st == 401:
                        if attempt == 1:
                            new_acc = ensure_token(ctx, acc)
                            if new_acc is not None:
                                acc = new_acc
                                continue
                        alt = pool.pick(client_key, session_id, respect_limit=True, exclude=acc["id"])
                        if alt is not None and attempt < 3:
                            sys.stderr.write("[failover] 账号 %s → %s（流式）\n" % (acc["id"], alt["id"]))
                            sys.stderr.flush()
                            pool.release(acc["id"])
                            acc = alt
                            pool.acquire(acc["id"])
                            continue
                    if st == 403 and _code_of(raw_err) == "4100406" and grd_level < guard_cap.MAX_LEVEL:
                        grd_level += 1
                        guard_cap.note_fail()
                        sys.stderr.write("[guard] 403 4100406 → 升级护栏到 level %d（流式）\n" % grd_level)
                        sys.stderr.flush()
                        payload = chat_cap.body(slot, messages, want_stream,
                                                level=grd_level, ctx=ctx, **kw)
                        continue
                    if st == 403 and _code_of(raw_err) == "4100404" and dev_ext is not True:
                        dev_ext = True
                        sys.stderr.write("[device] 403 4100404 风控 → 补发 Ual-Access-MarvisExt 重试（流式）\n")
                        sys.stderr.flush()
                        continue
                    if st == 403 and attempt == 1:
                        sys.stderr.write("[upstream] 403 首次命中，1.0s 后重试一次（流式）\n")
                        sys.stderr.flush()
                        time.sleep(1.0)
                        continue
                    api, code, msg = _classify(ctx, acc, st, raw_err)
                    if st == 401:
                        pool.on_unauthorized(acc["id"])
                    elif st == 429:
                        cd = pool.on_throttled(acc["id"])
                        sys.stderr.write("[throttle] 账号 %s 429 → 冷却 %ds（流式）\n"
                                         % (acc["id"], int(cd)))
                        sys.stderr.flush()
                    elif st >= 500:
                        pool.on_server_error(acc["id"])
                    _record(ctx, client_key, name, slot, acc, {}, 0, True, st)
                    pool.release(acc["id"])
                    raise api
                if grd_level > 0:
                    guard_cap.remember(ctx, grd_fp, grd_level)
                    guard_cap.note_ok()
                return ("stream", name, slot, acc, resp, {"budget_raised": raised})
            status, hdrs, raw, ms = chat_cap.call(acc, config, payload, ext=dev_ext,
                                                  meta=chat_cap.request_meta(slot))
        except Exception as e:
            pool.release(acc["id"])
            sys.stderr.write("[upstream] %s: %s | body_keys=%s\n" % (type(e).__name__, e, sorted(k for k in body)))
            sys.stderr.flush()
            if _is_net_error(e) and attempt < NET_RETRY_MAX:
                # 网络瞬断：账号本身没问题，原地退避重试，不换号、不罚号
                delay = NET_RETRY_DELAYS[min(attempt - 1, len(NET_RETRY_DELAYS) - 1)]
                sys.stderr.write("[net] 上游不可达，%.1fs 后重试（%d/%d）\n"
                                 % (delay, attempt, NET_RETRY_MAX - 1))
                sys.stderr.flush()
                time.sleep(delay)
                acc = pool.pick(client_key, session_id, respect_limit=True) or acc
                pool.acquire(acc["id"])
                continue
            raise ApiError(502, "连接上游失败：%s" % e, "upstream_unreachable", "server_error")

        if status == 200:
            if not want_stream and retried is None:
                need, new_budget = _should_retry(ctx, raw, hdrs, kw.get("max_tokens"))
                if need:
                    retried = {"budget_from": kw.get("max_tokens"), "budget_to": new_budget}
                    kw["max_tokens"] = new_budget
                    payload = chat_cap.body(slot, messages, want_stream, **kw)
                    continue
            pool.release(acc["id"])
            if grd_level > 0:
                guard_cap.remember(ctx, grd_fp, grd_level)
                guard_cap.note_ok()
            return ("json", name, slot, acc, raw, hdrs, ms, raised, retried, why)

        if status == 403 and _code_of(raw) == "4100406" and grd_level < guard_cap.MAX_LEVEL:
            grd_level += 1
            guard_cap.note_fail()
            sys.stderr.write("[guard] 403 4100406 → 升级护栏到 level %d\n" % grd_level)
            sys.stderr.flush()
            payload = chat_cap.body(slot, messages, want_stream, level=grd_level, ctx=ctx, **kw)
            continue
        if status == 403 and _code_of(raw) == "4100404" and dev_ext is not True:
            dev_ext = True
            sys.stderr.write("[device] 403 4100404 风控 → 补发 Ual-Access-MarvisExt 重试\n")
            sys.stderr.flush()
            continue
        if status == 403 and attempt == 1:
            # 上游偶发 403（像 WAF/风控），换一次机会，不计罚分
            sys.stderr.write("[upstream] 403 首次命中，1.0s 后重试一次\n")
            sys.stderr.flush()
            time.sleep(1.0)
            continue
        if status == 401:
            if attempt == 1:
                new_acc = ensure_token(ctx, acc)
                if new_acc is not None:
                    acc = new_acc
                    continue
            # 续期失败：换一个号再打一次（多号池的意义就在这里），最多换 2 次
            alt = pool.pick(client_key, session_id, respect_limit=True, exclude=acc["id"])
            if alt is not None and attempt < 3:
                sys.stderr.write("[failover] 账号 %s → %s\n" % (acc["id"], alt["id"]))
                sys.stderr.flush()
                pool.release(acc["id"])
                acc = alt
                pool.acquire(acc["id"])
                continue
        api, code, msg = _classify(ctx, acc, status, raw)
        if status == 401:
            pool.on_unauthorized(acc["id"])
        elif status == 429:
            cd = pool.on_throttled(acc["id"])
            sys.stderr.write("[throttle] 账号 %s 429 → 冷却 %ds\n" % (acc["id"], int(cd)))
            sys.stderr.flush()
        elif status >= 500:
            pool.on_server_error(acc["id"])
        _record(ctx, client_key, name, slot, acc, {}, 0, want_stream, status)
        pool.release(acc["id"])
        raise api


def build_json_response(ctx, name, slot, acc, raw, hdrs, ms, raised=False, retried=None, why=""):
    """把上游响应规整成对外 completion（含 SSE 聚合分支）。"""
    text = (raw or b"").decode("utf-8", "replace")
    if chat_cap.is_sse(hdrs, raw or b""):
        obj = aggregate_sse(text)
    else:
        try:
            obj = json.loads(text)
        except Exception:
            raise ApiError(502, "上游返回的不是 JSON：%s" % text[:160], "bad_upstream", "server_error")
    obj["model"] = name
    obj["id"] = obj.get("id") or ("chatcmpl-" + uuid.uuid4().hex[:24])
    obj["created"] = obj.get("created") or int(now())
    obj["_marvis"] = _marvis_meta(hdrs, slot, acc, ms)
    if raised:
        obj["_marvis"]["budget_raised"] = True
        obj["_marvis"]["budget_reason"] = why
    if retried:
        obj["_marvis"]["retried"] = True
        obj["_marvis"].update(retried)
    usage = obj.get("usage") or {}
    ctx["pool"].on_success(acc["id"])
    _record(ctx, "", name, slot, acc, usage, ms, False, 200)
    return obj


def aggregate_sse(text):
    """把 SSE 文本聚合成一条非流式 completion（含 tool_calls 拼装）。"""
    out = {"object": "chat.completion", "choices": []}
    content, reasoning = [], []
    finish = None
    tool_calls = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            obj = json.loads(data)
        except Exception:
            continue
        if obj.get("usage"):
            out["usage"] = obj["usage"]
        for ch in obj.get("choices") or []:
            d = ch.get("delta") or ch.get("message") or {}
            if d.get("content"):
                content.append(d["content"])
            if d.get("reasoning_content"):
                reasoning.append(d["reasoning_content"])
            for tc in d.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(idx, {"id": "", "type": "function",
                                                   "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            if ch.get("finish_reason"):
                finish = ch["finish_reason"]
    msg = {"role": "assistant", "content": "".join(content) or None}
    if reasoning:
        msg["reasoning_content"] = "".join(reasoning)
    if tool_calls:
        msg["tool_calls"] = [tool_calls[k] for k in sorted(tool_calls)]
        msg["content"] = None
    out["choices"] = [{"index": 0, "message": msg,
                       "finish_reason": finish or ("tool_calls" if tool_calls else "stop")}]
    return out


def stream_chunks(ctx, name, slot, acc, resp, client_key="", t0=None):
    """逐块转发上游 SSE，并把 id/model 改写成对外的。

    客户端断连时（serve 层捕获 BrokenPipe 后 close 本生成器）会走到 finally：
    关掉上游连接 → 上游请求被中止，不会在后台继续跑完。
    """
    rid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(now())
    t0 = t0 or now()
    first = True
    chars = 0          # 兜底估算（上游正常会在流里带 usage，见 usage_latest）
    reason_chars = 0
    usage_latest = None
    finished = False
    try:
        for kind, data in chat_cap.iter_sse(resp):
            if kind in ("ping", "timeout", "error"):
                # timeout 由 iter_sse 收尾：下游看到的就是「没有 [DONE] 的截断流」
                continue
            if kind == "done":
                break
            if kind == "other":
                continue
            try:
                obj = json.loads(data)
            except Exception:
                continue
            obj["id"] = rid
            obj["model"] = name
            obj["created"] = created
            if obj.get("usage"):
                usage_latest = obj["usage"]   # 上游每个 chunk 都带累计 usage，取最后一个即可
            if obj.get("choices"):
                d = obj["choices"][0].get("delta") or {}
                if d.get("content"):
                    chars += len(d["content"])
                if d.get("reasoning_content"):
                    reason_chars += len(d["reasoning_content"])
                if first:
                    d.setdefault("role", "assistant")
                    first = False
            yield "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"
        finished = True
        ctx["pool"].on_success(acc["id"])
        yield "data: [DONE]\n\n"
    finally:
        try:
            resp.close()
        except Exception:
            pass
        # 流式请求也要落一条用量（token 数按字符估算：中文约 2 字符 = 1 token）
        try:
            est = chars // 2 + reason_chars // 2
            if usage_latest:
                u = dict(usage_latest)
                u.setdefault("completion_tokens", est)
                u["total_tokens"] = int(u.get("prompt_tokens") or 0) + int(u.get("completion_tokens") or 0)
            else:
                u = {"completion_tokens": est, "total_tokens": est,
                     "completion_tokens_details": {"reasoning_tokens": reason_chars // 2}}
            # 注意 ms 统一用毫秒（非流式那边也是毫秒），之前这里直接传了秒，统计表里
            # 流式请求的耗时一直是 2.9 这种诡异小数
            _record(ctx, client_key, name, slot, acc, u, int((now() - t0) * 1000), True,
                    200 if finished else 0)
        except Exception:
            pass
        # 流结束（正常/断连）都要把并发名额还回去
        try:
            ctx["pool"].release(acc["id"])
        except Exception:
            pass


def _record(ctx, client_key, name, slot, acc, usage, ms, stream, status):
    try:
        ctx["store"].x(
            "INSERT INTO usage(at,iso,model,slot,account_id,prompt_tokens,completion_tokens,"
            "total_tokens,ms,stream,status,reasoning_tokens) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (now(), iso(), name, slot, acc["id"], int(usage.get("prompt_tokens") or 0),
             int(usage.get("completion_tokens") or 0), int(usage.get("total_tokens") or 0),
             ms, 1 if stream else 0, status,
             int(((usage.get("completion_tokens_details") or {}).get("reasoning_tokens")) or 0)))
    except Exception:
        pass


# ---------------- 生图 ----------------
IMAGE_MODEL = "marvis-image"

# 生图链路上游不按 model 路由（走 JSON-RPC metadata.capabilityId="vedas.image"），
# 库里这个字段纯粹是标签。为避免把对话模型名记进生图任务（按 model 统计会失真），
# 任何非生图标签一律归一成 IMAGE_MODEL。
IMAGE_MODEL_ALIASES = {"marvis-image", "marvis_image", "image", "marvis-image-1"}


def normalize_image_model(model):
    """返回 (归一后的标签, 是否发生过改写)。"""
    m = (model or "").strip()
    if not m:
        return IMAGE_MODEL, False
    if m.lower() in IMAGE_MODEL_ALIASES:
        return IMAGE_MODEL, m != IMAGE_MODEL
    return IMAGE_MODEL, True


def parse_size(size, allowed=None):
    """只接受正方形：上游按边长理解尺寸。

    allowed 非空时再做白名单校验——实测 1280×1280 连续两次都失败（约 4 分钟才回
    IMAGE_TASK_FAILED），与其让客户端白等，不如在入口直接 400。
    """
    raw = str(size or "1024").strip().lower().replace("*", "x")
    if "x" in raw:
        a, _, b = raw.partition("x")
        try:
            w, h = int(a), int(b)
        except ValueError:
            raise ApiError(400, "size 写法不对：%s（示例 1024x1024）" % size, "invalid_size")
        if w != h:
            raise ApiError(400, "只支持正方形尺寸（当前 %dx%d）" % (w, h), "invalid_size")
        n = w
    else:
        try:
            n = int(raw)
        except ValueError:
            raise ApiError(400, "size 写法不对：%s" % size, "invalid_size")
    if allowed and n not in allowed:
        raise ApiError(400, "不支持的尺寸 %d×%d（可用：%s）"
                       % (n, n, "、".join("%dx%d" % (x, x) for x in allowed)),
                       "invalid_size", param="size")
    return n


def images_generation(ctx, body, client_key=""):
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise ApiError(400, "prompt 不能为空", "invalid_prompt", param="prompt")
    cfg = ctx["config"]
    n = int(body.get("n") or 1)
    max_n = int(cfg.get("image_max_n") or 4)
    if n < 1 or n > max_n:
        raise ApiError(400, "n 取值范围 1~%d（上游一次任务只出一张，n>1 会拆成 N 个任务）" % max_n,
                       "unsupported_n", param="n")
    size = parse_size(body.get("size"), cfg.get("image_sizes") or None)
    raw_model = body.get("model")
    model, rewritten = normalize_image_model(raw_model)
    if rewritten:
        sys.stderr.write("[image] model 归一化 %r -> %s（生图按 capabilityId 路由，模型名仅作标签）\n"
                         % (raw_model, model))
    tasks = ctx["tasks"]
    items = [tasks.submit(prompt, str(size), client_key=client_key, model=model) for _ in range(n)]
    task = items[0]

    if body.get("wait"):
        deadline = now() + float(cfg.get("image_timeout") or 480)
        ids = [t["id"] for t in items]
        while now() < deadline:
            cur = [tasks.get(i) for i in ids]
            if all(c["status"] in ("completed", "failed") for c in cur):
                break
            time.sleep(1.5)
        cur = [tasks.get(i) for i in ids]
        ok = [c for c in cur if c["status"] == "completed"]
        if not ok:
            raise ApiError(502, "生图失败：%s" % (cur[0]["error"] or cur[0]["status"]),
                           "image_failed", "server_error")
        out = {"created": cur[0]["created"], "data": [{"url": c["data"][0]["url"]} for c in ok]}
        if len(ok) != n:
            out["errors"] = [{"id": c["id"], "error": c["error"] or c["status"]}
                             for c in cur if c["status"] != "completed"]
        return out

    if n == 1:
        task["status_url"] = "/v1/images/tasks/" + task["id"]
        return task
    # n>1：批量形状，每张单独可查
    return {
        "id": "batch_" + uuid.uuid4().hex[:16],
        "object": "image.generation.batch",
        "created": task["created"],
        "n": n,
        "size": str(size),
        "model": model,
        "items": [{**t, "status_url": "/v1/images/tasks/" + t["id"]} for t in items],
    }


def image_task(ctx, task_id):
    t = ctx["tasks"].get(task_id)
    if t is None:
        raise ApiError(404, "任务不存在：%s" % task_id, "task_not_found")
    return t
