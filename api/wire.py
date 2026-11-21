"""把本网关的地址写进各 Agent 的配置里，并且能原样退回去。

每个 agent 的配置格式都不同（JSON / TOML / 按插件 id 定位的 YAML），但要做的事
是同一个：把过去的值记下来 → 只动我们拥有的键 → 写回时保持原文件的字节风格。

对外只有四件事：
  agents()   —— 机器上装了哪些 agent，各自现在指向谁
  apply()    —— 切到本网关的某个模型
  rollback() —— 回到切之前的样子（逐字节）
  status()   —— 现在是谁记得我们改过

安全约定（这几条比功能重要）：
  * 不在 shell 环境变量里找 key。只认请求里给的 key，或者服务自己生成的那份。
  * 写入前重读文件并与快照比对，不一致就拒绝，不静默覆盖别人手改的内容。
  * 全部走临时文件 + fsync + rename，写到一半崩掉不会留下半个配置文件。
"""

import json
import os
import time

from core.agents import (
    CLAUDE_CODE,
    CODEX,
    DSH,
    ZCODE,
    build,
    build_all,
    names as agent_names,
    paths,
)
from core.agents.facts import Facts
from core.agents.snapshot import SnapshotStore

#: 控制台用：界面上要显示的名字与图标字。
LABELS = {
    CLAUDE_CODE: {"name": "Claude Code", "short": "CC", "glyph": "✳"},
    CODEX: {"name": "Codex", "short": "CX", "glyph": "◆"},
    DSH: {"name": "DeepSeek Harness", "short": "DSH", "glyph": "◈"},
    ZCODE: {"name": "ZCode", "short": "ZC", "glyph": "◇"},
}

#: 单次请求最多返回多少行用量明细，防止一次把几十万行塞进浏览器。
USAGE_LIMIT = 100

#: 上一版「没 key 就写占位符」留下的标记，现在只用于给老配置一句解释。
#:
#: 现在的做法是 api/keys.wire_key()：服务自己发一把 mkx_ 真钥匙。占位符的问题
#: 是接完线 agent 第一次对话必然 401，用户还得自己建 key 再重接一次；真钥匙
#: 则一次接完就能用。这个常量留着，是为了让已经写过占位符的老配置被重新
#: 接线时能认出它、并告诉用户「这是旧版的占位符」。
PENDING_KEY = "<未配置 API Key>"


class WireError(Exception):
    """请求本身没问题，但做不了（没装这个 agent、配置被手改过……）。"""

    def __init__(self, message, code="wire_error", extra=None):
        super().__init__(message)
        self.code = code
        self.extra = extra or {}


# ---- 网关事实 -------------------------------------------------------------

def facts_from_ctx(ctx):
    """从本服务自己的 /v1/provider 口径里取事实，别让适配器去猜。

    base_url 一律取 provider_block 里的那份：它是唯一权威的自我描述，
    命令行参数和 config.json 都可能被改得跟实际监听对不上。
    """
    provider = _provider(ctx)
    models = ctx.get("models") or {}
    return Facts(models, provider)


def _provider(ctx):
    """本服务的自我描述块。serve.py 里每个请求现算，这里复用同一个口径。"""
    from serve import provider_block
    return provider_block(ctx, full=False)


def base_url_of(ctx):
    provider = _provider(ctx)
    url = provider.get("base_url") or ""
    if not url:
        cfg = ctx.get("config") or {}
        host = cfg.get("host") or "127.0.0.1"
        port = cfg.get("port") or 8088
        return "http://%s:%s/v1" % (host, port)
    return url


def gateway_key_of(ctx, given=None):
    """这次要写进配置的 key。

    优先级：请求显式给的 > 请求实际带上并已通过鉴权的 key > 服务自己发的那把 mkx_。

    绝不读环境变量 —— 环境里的 key 是给「当前 shell 里跑的那个 agent」用的，
    写进配置文件就等于把它固化了，下次换 key 时会有两个来源互相打架。

    也绝不兜底成「看起来像真的」字面量：早年这里 `return "marvis-proxy"` 兜底，
    结果是接完线的 agent 拿一个不存在的 key 去请求，必然 401 —— 而 apply 还回报
    成功，排查时根本想不到配置里躺着一句假 key。

    最后那档走 api/keys.wire_key()：服务自己发一把 mkx_ 存进库再写进配置。
    这样「刚装完 agent、手上没有任何 key」的人，一键接入完就是**真能用**的，
    不需要先建 key 再重接一次；而配置里那把是库里的真 key，agent 拿它说话
    完全正常，不会像占位符那样第一次对话就 401。
    """
    if given:
        return str(given)
    stored = ctx.get("wire_key")
    if stored:
        return stored
    from api import keys as keys_api
    return keys_api.wire_key(ctx)


def key_is_known(ctx, key):
    """这把 key 在库里能不能查到。

    只用来**提醒**，不用来拦截：请求体里显式给的 key 允许是别的机器的、别人给的、
    或者是刚在另一处建好还没同步过来的。但配置写完之后 agent 会拿它去请求上游，
    写错了要到第一次对话才 401，排查起来很费劲 —— 所以这里先查一眼，查不到就
    在 warnings 里说清楚。
    """
    if not key:
        return False
    try:
        from api import keys as keys_api
        return keys_api.lookup(ctx, str(key)) is not None
    except Exception:                                  # noqa: BLE001
        # 查不了就当它「可能有效」，别因为库读失败又把正常流程拦下来
        return True


def _key_warning(ctx, key):
    """写进配置的 key 需要提一句的，返回那句话；没有就返回空串。

    三种情况：
      · 是服务自己发的 mkx_：说明它是什么（用户会在配置里看到这个前缀，得知道来路）；
      · 用户自己的 key 且库里能查到：什么都不说，正常；
      · 查不到：可能是别的机器的 / 手误填的，提示一句但不拦。
    """
    token = str(key)
    if token.startswith(PENDING_KEY):
        # 兜底：理论上不会走到了（gateway_key_of 已改成发真钥匙）
        return ("配置已写好，但里面还没有 API Key。"
                "建一把再重新接一次就会被覆盖：python tools/admin.py key new --note 我的机器")
    if token.startswith("mkx_"):
        return ("配置里写的是本服务自动发放的接入钥匙（mkx_ 开头，末尾 …%s）。"
                "它是库里的一把真 key，agent 可以直接用；它不出现在 key list 里，"
                "也不需要管。" % token[-6:])
    if key_is_known(ctx, key):
        return ""
    return ("写进配置的 key 不在本服务的库里（末尾 …%s）。如果它是别的机器 / 别人给的，"
            "忽略这条；如果是手误填的，agent 第一次对话就会 401。"
            % (token[-6:] if len(token) > 6 else ""))


# ---- 适配器装配 -----------------------------------------------------------

def _data_dir(ctx):
    return ctx.get("data_dir") or (ctx.get("config") or {}).get("data_dir") or os.path.join(os.getcwd(), "data")


def _store(ctx):
    """快照和原文件备份都放服务自己的 data 旁边，别写进 agent 的目录里。"""
    return SnapshotStore(ctx["store"], os.path.join(_data_dir(ctx), "..", "wire-backup"))


def _build(ctx, agent_id, home, key=None, need_key=True):
    """装配一个适配器。

    need_key=False 时只用于「看一眼现在是什么状态」（read-only）：这种调用根本不会
    写文件，key 有没有都不影响能不能读出配置。早期这里无脑走 gateway_key_of()，
    结果一把 key 没配（或服务重启后 ctx 里还没填）时，/v1/wire/agents 会整页变成
    四条错误记录，界面上只剩 id —— 一个纯读接口不该被写接口的前置条件挡住。
    """
    if need_key:
        wired_key = gateway_key_of(ctx, key)
    else:
        # 读场景给个占位符即可：适配器只在读配置，不会把它写进任何文件
        wired_key = str(key or ctx.get("wire_key") or "-")
    return build(
        agent_id,
        home=home,
        gateway_url=base_url_of(ctx),
        gateway_key=wired_key,
        store=_store(ctx),
        facts=facts_from_ctx(ctx),
    )


def _home(override=None):
    return paths.abs(override or paths.home())


# ---- 对外动作 -------------------------------------------------------------

def _clean(exc):
    """把适配器抛出来的异常变成一句人话。

    文件被外部改过时，适配器抛的是 DriftError —— 它不是故障，是提醒，
    应该让调用方看到「用 force=true 覆盖，或先回滚」而不是一串类名。
    """
    from core.agents.base import DriftError
    if isinstance(exc, DriftError):
        return str(exc)
    return "%s: %s" % (type(exc).__name__, exc)


def _result_dict(result):
    out = {"agent": result.agent}
    for name in ("changed", "skipped", "warnings", "needs_restart",
                 "files_created", "restored", "removed"):
        value = getattr(result, name, None)
        if value or name == "needs_restart":
            out[name] = value
    return out


def agents(ctx, home=None):
    """装了哪些 agent、各自指向哪个 base_url。只读，不改任何文件。"""
    root = _home(home)
    store = _store(ctx)
    gateway = base_url_of(ctx)
    out = []
    for agent_id in agent_names():
        try:
            adapter = _build(ctx, agent_id, root, need_key=False)
        except WireError as exc:
            # 只读接口不该因为「没配 key」整页 500：把原因挂到每个 agent 上
            out.append({"id": agent_id, "error": str(exc), "code": exc.code})
            continue
        except Exception as exc:                      # noqa: BLE001 - 只做展示
            out.append({"id": agent_id, "error": str(exc)})
            continue
        label = LABELS.get(agent_id) or {}
        snap = store.load(agent_id)
        try:
            info = {"id": agent_id,
                    "display_name": adapter.display_name or label.get("name") or agent_id,
                    "short": label.get("short") or agent_id,
                    "glyph": label.get("glyph") or "·",
                    "installed": adapter.present(),
                    "files": [str(p) for p in adapter.all_paths()],
                    "needs_restart": adapter.needs_restart,
                    "wired": snap is not None,
                    "applied": (snap or {}).get("applied"),
                    "snapshot_at": (snap or {}).get("created_at")}
        except Exception as exc:                      # noqa: BLE001 - 一个 agent 判断不了不能拖垮整页
            # 典型是 DSH 的 ProfileError：机器上有多个 profile 又判断不出该用哪个。
            # 这是「这个 agent 现在没法处理」，不是服务故障，不该 500。
            out.append({"id": agent_id, "error": str(exc),
                        "code": getattr(exc, "code", "unavailable"),
                        "display_name": label.get("name") or agent_id,
                        "short": label.get("short") or agent_id,
                        "glyph": label.get("glyph") or "·",
                        "installed": False, "files": [], "wired": False,
                        "state": "absent"})
            continue
        try:
            info["current"] = _current(adapter)
            info["warnings"] = adapter.extra_warnings()
        except Exception as exc:                      # noqa: BLE001 - 只做展示
            info["warnings"] = ["读取当前配置失败：%s" % exc]
        info["state"] = _state_of(info, gateway)
        out.append(info)
    return {"ok": True, "home": str(root), "gateway": gateway, "agents": out}


def _state_of(info, gateway):
    """三态，界面只靠这个画圆点：off / stale / live。

    stale = 我们记着改过它，但现在配置里的地址已经指别处（被别的工具改回去了）。
    这比「有没有快照」更能说明真实处境，所以放在前面算。
    """
    if not info.get("installed"):
        return "absent"
    if not info.get("wired"):
        return "off"
    base = ((info.get("current") or {}).get("base_url") or "").rstrip("/")
    if base and not _same_gateway(base, gateway):
        return "stale"
    return "live"


def _same_gateway(base, gateway):
    strip = lambda u: (u or "").rstrip("/").lower()
    a, b = strip(base), strip(gateway)
    return a == b or a + "/v1" == b or a == b + "/v1"


def _current(adapter):
    """现在这些文件里写的是哪个模型/地址 —— 给人看的摘要。"""
    summary = {"model": None, "base_url": None}
    for path in adapter.all_paths():
        if not path.exists():
            continue
        if path.name == "settings.json":
            data = json.loads(path.read_text(encoding="utf-8", errors="surrogateescape") or "{}")
            env = data.get("env") or {}
            summary["base_url"] = env.get("ANTHROPIC_BASE_URL")
            summary["model"] = data.get("model")
        elif path.name == "config.toml":
            from core.agents import edit
            text = path.read_text(encoding="utf-8", errors="surrogateescape")
            summary["model"] = edit.toml_get_value(text, "model", None)
            summary["base_url"] = edit.toml_get_value(text, "openai_base_url", None)
    return summary


# ---- 控制台要的只读数据 ---------------------------------------------------
# 这几个函数只读不写，缺数据就给空列表 —— 控制台某一块没数据不该让整页空白。

def models(ctx):
    """可选的模型。字段按控制台要的直接摊平，前端不再解析四层嵌套。"""
    facts = facts_from_ctx(ctx)
    out = []
    for m in facts.models:
        out.append({
            "id": m.id,
            "name": m.display,
            "slot": m.slot,
            "aliases": list(m.alts),
            "context_window": m.context_window,
            "max_output_tokens": m.max_output_tokens,
            "caps": m.caps,
        })
    return {"ok": True, "gateway": base_url_of(ctx),
            "anthropic_url": facts.anthropic_url, "openai_url": facts.openai_url,
            "models": out}


def usage(ctx, days=7, by="day"):
    """用量聚合。直接复用 admin 的那套 SQL，避免两处口径不一致。"""
    from api import admin as admin_api
    by = by if by in ("day", "model", "account") else "day"
    try:
        days = max(0.5, min(float(days or 7), 90.0))
    except (TypeError, ValueError):
        days = 7.0
    try:
        return admin_api.usage_report(ctx, days, by)
    except Exception as exc:                          # noqa: BLE001 - 控制台降级
        return {"ok": False, "error": str(exc), "items": [],
                "total": {"requests": 0, "total_tokens": 0}}


def recent(ctx, limit=40):
    """最近若干轮请求。平时没人看，点心形统计时才拉，所以单独一个口。"""
    limit = max(1, min(int(limit or 40), USAGE_LIMIT))
    rows = ctx["store"].q("SELECT * FROM usage ORDER BY id DESC LIMIT ?", (limit,))
    keep = ("id", "at", "model", "status", "stream", "prompt_tokens", "completion_tokens",
            "total_tokens", "reasoning_tokens", "ms")
    items = []
    for row in rows:
        rec = dict(row)
        rec["at"] = rec.get("at")
        items.append({k: rec.get(k) for k in keep})
    return {"ok": True, "items": items}


def keys(ctx):
    """钥匙清单。**只给前缀和计数，绝不回传可用 key。**"""
    try:
        from api import keys as keys_api
        payload = keys_api.list_keys(ctx)
    except Exception as exc:                          # noqa: BLE001 - 控制台降级
        return {"ok": False, "error": str(exc), "keys": []}
    rows = []
    for k in payload.get("keys") or []:
        rows.append({"id": k.get("id"), "prefix": k.get("prefix"), "note": k.get("note"),
                     "enabled": bool(k.get("enabled")), "uses": k.get("uses"),
                     "created": k.get("created"), "last_used": k.get("last_used")})
    return {"ok": True, "required": payload.get("required"), "keys": rows}


def health(ctx):
    """给页头用的一行状态：进程、账号池、用量兜底。"""
    try:
        from api import admin as admin_api
        return admin_api.health(ctx)
    except Exception as exc:                          # noqa: BLE001
        return {"ok": False, "error": str(exc)}


# ---- 观测页要的几块（都只读，且都不含凭证明文） ---------------------------
# 这些数据原本挂在 /admin/* 下，要 admin_token。控制台把它们收进 /v1/wire/observe，
# 由 serve.py 在调用前先过一遍 admin 鉴权 —— 鉴权留在路由层，这里只管取数。

def accounts(ctx):
    """账号池。openid 已由 admin 侧打码，token 长度只给数字。"""
    try:
        from api import admin as admin_api
        return admin_api.accounts(ctx)
    except Exception as exc:                          # noqa: BLE001
        return {"ok": False, "error": str(exc), "accounts": [], "counts": {}}


def guard(ctx):
    """护栏现状：规则、策略、记忆条数、熔断。"""
    try:
        from api import admin as admin_api
        return admin_api.sanitize_info(ctx)
    except Exception as exc:                          # noqa: BLE001
        return {"ok": False, "error": str(exc), "rules": [], "plans": []}


def backups(ctx):
    """备份目录里有什么。只列文件名和大小，不读内容。"""
    import glob
    out_dir = os.path.abspath(os.path.join(_data_dir(ctx), "..", "backups"))
    try:
        files = sorted(glob.glob(os.path.join(out_dir, "marvis-*.tar.gz")))
    except OSError:
        files = []
    rows = []
    for path in files:
        try:
            rows.append({"name": os.path.basename(path), "size": os.path.getsize(path)})
        except OSError:
            continue
    return {"ok": True, "dir": out_dir, "last": ctx.get("last_backup"), "files": rows}


def trace(ctx, limit=20, minutes=60):
    """最近 N 轮请求摘要。不含票据、不含正文。"""
    try:
        from api import admin as admin_api
        return admin_api.trace(ctx, limit=int(limit or 20), minutes=float(minutes or 60))
    except Exception as exc:                          # noqa: BLE001
        return {"ok": False, "error": str(exc), "items": []}


def observe(ctx, body=None):
    """观测页一次要的全部数据。任何一块失败都不影响其余块。"""
    body = body or {}
    return {
        "ok": True,
        "accounts": accounts(ctx),
        "guard": guard(ctx),
        "backups": backups(ctx),
        "trace": trace(ctx, body.get("limit") or 20, body.get("minutes") or 60),
        "usage": usage(ctx, body.get("days") or 7, body.get("by") or "day"),
        "recent": recent(ctx, body.get("recent") or 40),
    }



def apply_model(ctx, model, effort=None, agent_ids=None, home=None, force=False, key=None,
                profile=None):
    """把选中的 agent 全部切到 `model`，写到本网关。

    ``profile`` 只对 DSH 有意义：一台机器上常有 ``desktop`` 和 ``web`` 两份 profile，
    不指定时适配器会按环境变量推断，推断不出来就报错而不是猜。
    """
    root = _home(home)
    targets = list(agent_ids or [])
    unknown = [a for a in targets if a not in agent_names()]
    if unknown:
        raise WireError("未知 agent：%s；可选：%s" % (", ".join(unknown), ", ".join(agent_names())),
                        code="unknown_agent")
    facts = facts_from_ctx(ctx)
    target = facts.find(model)
    if target is None:
        raise WireError(
            "不存在模型 %r；可选：%s" % (model, ", ".join(m.id for m in facts.models)),
            code="unknown_model",
        )
    results, skipped = [], []
    for agent_id in agent_names():
        if targets and agent_id not in targets:
            continue
        try:
            adapter = _build(ctx, agent_id, root, key)
            if profile and agent_id == "dsh":
                adapter.profile_hint = profile
        except Exception as exc:                      # noqa: BLE001 - 一个坏不能拖垮全部
            results.append({"agent": agent_id, "error": _clean(exc),
                            "changed": [], "skipped": [], "warnings": []})
            continue
        if not targets and not adapter.present():
            skipped.append(agent_id)
            continue
        try:
            results.append(_result_dict(adapter.apply(target.id, effort, force=force)))
        except Exception as exc:                      # noqa: BLE001 - 一个坏不能拖垮全部
            results.append({"agent": agent_id, "error": _clean(exc),
                            "changed": [], "skipped": [], "warnings": []})
    # gateway_key_of 现在不会抛了（拿不到就返回占位符），这里不再需要 try。
    top_warnings = []
    _kw = _key_warning(ctx, gateway_key_of(ctx, key))
    if _kw and results:
        top_warnings.append(_kw)

    return {"ok": True, "model": target.id, "display": target.display,
            "gateway": base_url_of(ctx), "effort": effort,
            "results": results, "not_installed": skipped,
            "warnings": top_warnings}


def rollback(ctx, agent_ids=None, home=None, force=False):
    """回到切换之前。逐字节，不是「把看起来像我们写的行删掉」。"""
    root = _home(home)
    targets = list(agent_ids or [])
    unknown = [a for a in targets if a not in agent_names()]
    if unknown:
        raise WireError("未知 agent：%s；可选：%s" % (", ".join(unknown), ", ".join(agent_names())),
                        code="unknown_agent")
    store = _store(ctx)
    results, untouched = [], []
    for agent_id in agent_names():
        if targets and agent_id not in targets:
            continue
        if not store.exists(agent_id):
            untouched.append(agent_id)
            continue
        try:
            # 回滚是「把文件还原」，不写任何 key，所以不需要 key
            adapter = _build(ctx, agent_id, root, need_key=False)
        except Exception as exc:                      # noqa: BLE001 - 同上
            results.append({"agent": agent_id, "error": _clean(exc),
                            "restored": [], "removed": [], "warnings": []})
            continue
        try:
            results.append(_result_dict(adapter.rollback(force=force)))
        except Exception as exc:                      # noqa: BLE001 - 同上
            results.append({"agent": agent_id, "error": _clean(exc),
                            "restored": [], "removed": [], "warnings": []})
    return {"ok": True, "results": results, "untouched": untouched}


def status(ctx, home=None):
    store = _store(ctx)
    out = []
    for agent_id in agent_names():
        if not store.exists(agent_id):
            continue
        snap = store.load(agent_id) or {}
        out.append({
            "agent": agent_id,
            "created_at": snap.get("created_at"),
            "applied": snap.get("applied"),
            "files": sorted((snap.get("files") or {}).keys()),
        })
    return {"ok": True, "gateway": base_url_of(ctx), "wired": out,
            "known_agents": list(agent_names())}


# ---- HTTP 门面 ------------------------------------------------------------
# 只读的口不用 key（和 /v1/provider 一个待遇），会改文件的口必须带 key：
# 这个接口能重写用户 ~/.claude、~/.codex 下的配置，不能让它对内网任意进程敞开。

WRITE_ACTIONS = ("apply", "rollback")

#: 只读动作 -> 处理函数。控制台刷新一次要打四五个口，这里统一成一张表。
READ_ACTIONS = {
    "agents": lambda ctx, body: agents(ctx, home=body.get("home")),
    "status": lambda ctx, body: status(ctx, home=body.get("home")),
    "models": lambda ctx, body: models(ctx),
    "usage": lambda ctx, body: usage(ctx, body.get("days") or 7, body.get("by") or "day"),
    "recent": lambda ctx, body: recent(ctx, body.get("limit") or 40),
    "keys": lambda ctx, body: keys(ctx),
    "health": lambda ctx, body: health(ctx),
    "observe": lambda ctx, body: observe(ctx, body),
}

#: 需要 admin_token 的只读动作。它们能看到账号池、护栏规则和链路摘要，
#: 比「装了哪些 agent」敏感得多，所以不能跟 agents 一个待遇。
ADMIN_ACTIONS = ("observe",)


def handle(ctx, action, body=None):
    """给 serve.py 调：返回 (status_code, payload)。

    action: agents / status / models / usage / recent / keys / health / apply / rollback
    body:   {"model": "...", "effort": "...", "agents": [...], "home": "...",
             "force": false, "key": "...", "days": 7, "by": "day", "limit": 40}
    """
    body = body or {}
    try:
        if action in READ_ACTIONS:
            return 200, READ_ACTIONS[action](ctx, body)
        if action == "apply":
            if not body.get("model"):
                raise WireError("缺少 model", code="bad_request")
            return 200, apply_model(
                ctx, body["model"], effort=body.get("effort"),
                agent_ids=body.get("agents"), home=body.get("home"),
                force=bool(body.get("force")), key=body.get("key"),
                profile=body.get("profile"),
            )
        if action == "rollback":
            return 200, rollback(ctx, agent_ids=body.get("agents"),
                                 home=body.get("home"), force=bool(body.get("force")))
        raise WireError("未知动作 %r；可选：%s" % (action, ", ".join(sorted(READ_ACTIONS) + list(WRITE_ACTIONS))),
                        code="unknown_action")
    except WireError as exc:
        payload = {"ok": False, "error": str(exc), "code": exc.code}
        payload.update(exc.extra)
        return 400, payload
    except Exception as exc:                          # noqa: BLE001 - 别把栈丢给客户端
        return 500, {"ok": False, "error": _clean(exc), "code": "internal_error"}


def describe():
    """给 /v1/provider 的 endpoints 用。"""
    return {
        "wire_agents": "/v1/wire/agents",
        "wire_status": "/v1/wire/status",
        "wire_models": "/v1/wire/models",
        "wire_usage": "/v1/wire/usage",
        "wire_recent": "/v1/wire/recent",
        "wire_keys": "/v1/wire/keys",
        "wire_health": "/v1/wire/health",
        "wire_apply": "/v1/wire/apply",
        "wire_rollback": "/v1/wire/rollback",
        "console": "/",
    }
