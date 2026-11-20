# -*- coding: utf-8 -*-
"""管理面：加号（出链接 / 收回填）、账号池、用量、清理。只监听 127.0.0.1。"""
import json
import os
import time
import uuid

from cap import chat as chat_cap
from cap import login as login_cap
from cap import guard as guard_cap
from cap import uskey as uskey_cap
from core.store import iso, now


class AdminError(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status
        self.message = message


# ---------------- 加号 ----------------
def login_start(ctx, body=None):
    store = ctx["store"]
    lt = str((body or {}).get("type") or (body or {}).get("login_type") or "QC").upper()
    if lt not in login_cap.LOGIN_TYPE:
        raise AdminError(400, "type 只能是 QC 或 WX")
    state = uuid.uuid4().hex[:8]
    guid = uuid.uuid4().hex
    store.x("INSERT OR REPLACE INTO login_pending(state,guid,login_type,at) VALUES(?,?,?,?)",
            (state, guid, lt, now()))
    store.x("DELETE FROM login_pending WHERE state NOT IN "
            "(SELECT state FROM login_pending ORDER BY at DESC LIMIT 10)")
    hint = ("这个链接在手机浏览器里打开 → 登录 → 授权 → 复制落地页的整段内容（JSON 正文优先，"
            "其次是地址栏整条 URL）→ POST /admin/login/exchange {\"text\": \"...\"}")
    if lt == "WX":
        hint = ("微信登录：把链接发到另一台设备用微信扫码/打开（同机浏览器通常会要求「用微信打开」）"
                "→ 授权后同样把落地页整段内容回填 POST /admin/login/exchange")
    return {
        "ok": True, "state": state, "guid": guid,
        "login_type": lt,
        "url": login_cap.authorize_url(state, lt),
        "hint": hint,
        "expires_note": "本链接约 5 分钟有效，别重复点 start（会换 state）",
    }


def login_exchange(ctx, body):
    text = (body.get("text") or body.get("url") or body.get("code") or "").strip()
    if not text:
        raise AdminError(400, "text 不能为空：把落地页整段内容或地址栏 URL 整个发过来")
    store, pool = ctx["store"], ctx["pool"]

    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except Exception as e:
            raise AdminError(400, "看起来是 JSON 但解析失败：%s" % e)
        info = login_cap._normalize(obj.get("user_info") or obj.get("userInfo") or {})
        if not info:
            raise AdminError(400, "这段 JSON 里没有 user_info/access_token")
        return _save(ctx, info, "paste-json")

    code, state = login_cap.parse_callback(text)
    if not code:
        raise AdminError(400, "没找到 code：确认复制的是落地页地址栏，或整段 JSON")
    pend = None
    if state:
        pend = store.q1("SELECT * FROM login_pending WHERE state=?", (state,))
    if pend is None:
        pend = store.q1("SELECT * FROM login_pending ORDER BY at DESC LIMIT 1")
    guid = (pend["guid"] if pend else "") or uuid.uuid4().hex
    login_type = pend["login_type"] if pend else "QC"

    r = login_cap.exchange(code, state, guid, login_type)
    if not r.get("ok"):
        raise AdminError(400, "兑换失败：%s (status=%s code=%s msg=%s)"
                         % (r.get("error"), r.get("status"), r.get("code"), r.get("msg")))
    info = r["info"]
    info["guid"] = guid
    info["login_type"] = login_type
    if pend:
        store.x("DELETE FROM login_pending WHERE state=?", (pend["state"],))
    return _save(ctx, info, "exchange-url")


def _save(ctx, info, source):
    pool = ctx["pool"]
    if not info.get("openid") or not info.get("access_token"):
        raise AdminError(400, "缺 openid 或 access_token")
    acc_id = pool.add_account(info, note="added by " + source)
    row = pool.get(acc_id)
    probe = chat_cap.probe({"openid": row["openid"], "access_token": row["access_token"],
                            "guid": row["guid"], "login_type": row["login_type"]}, ctx["config"])
    return {"ok": True, "id": acc_id, "openid": (row["openid"] or "")[:6] + "…",
            "token_len": len(row["access_token"] or ""),
            "has_refresh": bool(row["refresh_token"]),
            "probe": probe, "source": source}


# ---------------- 账号池 ----------------
def accounts(ctx, body=None):
    pool = ctx["pool"]
    rows = pool.masked()
    for r in rows:
        r["inflight"] = pool.inflight(r["id"])
    return {"ok": True, "counts": pool.counts(), "accounts": rows}


def account_action(ctx, account_id, action, body=None):
    pool, config = ctx["pool"], ctx["config"]
    row = pool.get(account_id)
    if row is None:
        raise AdminError(404, "账号不存在：%s" % account_id)
    if action == "probe":
        return {"ok": True, "probe": chat_cap.probe(
            {"openid": row["openid"], "access_token": row["access_token"],
             "guid": row["guid"], "login_type": row["login_type"]}, config)}
    if action == "refresh":
        from api.openai import ensure_token
        new_row = ensure_token({"pool": pool, "store": ctx["store"], "config": config}, row)
        if new_row is None:
            raise AdminError(400, "续期失败（可能 refresh_token 已失效，需要重新加号）")
        return {"ok": True, "refreshed": True, "token_len": len(new_row["access_token"] or "")}
    if action == "disable":
        pool.set_status(account_id, "disabled")
        return {"ok": True, "status": "disabled"}
    if action == "enable":
        pool.set_status(account_id, "active")
        return {"ok": True, "status": "active"}
    if action == "delete":
        pool.delete(account_id)
        return {"ok": True, "deleted": account_id}
    raise AdminError(400, "未知动作：%s（probe/refresh/disable/enable/delete）" % action)


# ---------------- 其它 ----------------
def usage(ctx, limit=50):
    rows = ctx["store"].q("SELECT * FROM usage ORDER BY id DESC LIMIT ?", (int(limit),))
    return {"ok": True, "items": [dict(r) for r in rows]}


REPORT_BUCKETS = {
    "day": "strftime('%Y-%m-%d', at, 'unixepoch', 'localtime')",
    "model": "model",
    "account": "CAST(account_id AS TEXT)",
}


def usage_report(ctx, days=7, by="day"):
    """按天 / 按模型 / 按账号聚合。纯 SQL GROUP BY，不需要新表。"""
    if by not in REPORT_BUCKETS:
        raise AdminError(400, "by 只能是 day / model / account")
    expr = REPORT_BUCKETS[by]
    since = now() - float(days) * 86400
    rows = ctx["store"].q(
        "SELECT {e} AS bucket, COUNT(*) AS requests,"
        " COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
        " COALESCE(SUM(completion_tokens),0) AS completion_tokens,"
        " COALESCE(SUM(total_tokens),0) AS total_tokens,"
        " COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens,"
        " CAST(COALESCE(AVG(ms),0) AS INTEGER) AS avg_ms,"
        " COALESCE(SUM(CASE WHEN status>=400 THEN 1 ELSE 0 END),0) AS errors,"
        " COALESCE(SUM(CASE WHEN status=429 THEN 1 ELSE 0 END),0) AS throttled,"
        " COALESCE(SUM(CASE WHEN stream=1 THEN 1 ELSE 0 END),0) AS streaming"
        " FROM usage WHERE at >= ? GROUP BY bucket ORDER BY bucket".format(e=expr),
        (since,))
    items = [dict(r) for r in rows]
    for it in items:                       # p95 单独取，避免在 SQL 里算分位
        b = ctx["store"].q(
            "SELECT ms FROM usage WHERE at >= ? AND {e} IS NOT NULL AND {e} = ?"
            " ORDER BY ms".format(e=expr), (since, it["bucket"]))
        vals = [x["ms"] for x in b if x["ms"] is not None]
        it["p95_ms"] = vals[int(len(vals) * 0.95)] if vals else 0
    tot = ctx["store"].q1(
        "SELECT COUNT(*) AS requests, COALESCE(SUM(total_tokens),0) AS total_tokens"
        " FROM usage WHERE at >= ?", (since,))
    return {"ok": True, "days": days, "by": by, "since": int(since), "items": items,
            "total": dict(tot) if tot else {"requests": 0, "total_tokens": 0}}


TRACE_FIELDS = ("at", "time", "model", "slot", "prompt_tokens",
                "completion_tokens", "reasoning_tokens", "total_tokens", "ms",
                "stream", "status", "kind")


def trace(ctx, limit=20, minutes=60):
    """最近 N 轮请求摘要，给「另一个人 / 另一个 agent」看链路用。

    只读 usage 表。**不含** uskey/票据（按用户要求不加），**不含** prompt 正文，
    **不含** openid / access_token / 内部 account_id —— 只给账号备注当提示，
    避免把账号指纹带出去。
    """
    limit = max(1, min(int(limit or 20), 200))
    try:
        minutes = float(minutes or 60)
    except (TypeError, ValueError):
        minutes = 60.0
    since = now() - minutes * 60
    rows = ctx["store"].q(
        "SELECT u.at AS at, u.iso AS time, u.model AS model, u.slot AS slot,"
        " u.prompt_tokens AS prompt_tokens, u.completion_tokens AS completion_tokens,"
        " u.reasoning_tokens AS reasoning_tokens, u.total_tokens AS total_tokens,"
        " u.ms AS ms, u.stream AS stream, u.status AS status, u.kind AS kind,"
        " a.note AS acc_hint, a.id AS acc_id"
        " FROM usage u LEFT JOIN accounts a ON a.id = u.account_id"
        " WHERE u.at >= ? ORDER BY u.id DESC LIMIT ?", (since, limit))
    items = []
    for r in rows:
        d = dict(r)
        hint = (d.pop("acc_hint", "") or "").strip().lower()
        d.pop("acc_id", None)
        # 备注只在「是正常人写的说明」时才外露；"recon 实测链路取得的凭证"、
        # "added by exchange-url" 这类是实现细节/取证痕迹，一律折成 account。
        if hint and "recon" not in hint and "added by" not in hint:
            d["account"] = hint[:24]
        else:
            d["account"] = "account"
        items.append({k: d.get(k) for k in TRACE_FIELDS + ("account",)})
    acc = ctx["store"].q1("SELECT COUNT(*) n FROM accounts")
    return {
        "ok": True,
        "provider": ctx["config"].get("provider_name") or "Marvis Proxy",
        "upstream": "yybadaccess.3g.qq.com",
        "window_minutes": minutes,
        "limit": limit,
        "count": len(items),
        "accounts_total": acc["n"] if acc else 0,
        "items": items,
        "how_to_read": {
            "route": "客户端请求 /v1/chat/completions 带 model=你传的名字 → "
                     "slot 是解析出的槽位 → 上游按槽位路由到真实模型",
            "model": "客户端传进来（或回显）的名字，包含友好别名",
            "slot": "内部槽位名（main-auto / deepseek-v4-pro-external / …）",
            "paused": "本接口不提供票据(uskey)、prompt 正文、openid/access_token",
        },
        "note": "只含请求摘要：不含票据(uskey)、不含 prompt 正文、不含凭证明文。",
    }


def health(ctx):
    pool = ctx["pool"]
    tasks = ctx["store"].q1("SELECT COUNT(*) n FROM tasks WHERE state IN ('queued','submitted','working')")
    rows = pool.masked()
    return {
        "ok": True, "time": iso(), "pid": os.getpid(),
        "accounts": pool.counts(),
        "accounts_expiring": len([r for r in rows if r.get("expiry_warn")]),
        "accounts_expired": pool.counts().get("expired", 0),
        "models": [a["name"] for a in ctx["models"].get("aliases") or []],
        "tasks_running": tasks["n"] if tasks else 0,
        "inflight": pool.total_inflight(),
        "last_cleanup": ctx.get("last_cleanup"),
        "uskey": _uskey_brief(),
        "bind": _bind_brief(ctx),
        "version": "0.1.0",
    }


def _bind_brief(ctx):
    """监听地址一览。host 若是 127.0.0.1，手机就连不上——这里能一眼看出来。

    config.json 的 host 曾被外部改写回 127.0.0.1，导致手机那条路静默失效，
    所以把它放进 /health，每次核对都不必再去翻配置文件。
    """
    cfg = ctx.get("config") or {}
    host = str(cfg.get("host") or "")
    port = cfg.get("port")
    lan_ok = host in ("0.0.0.0", "::", "")
    return {"host": host, "port": port, "lan_reachable": lan_ok}


def _uskey_brief():
    """出票后端一览。ticket ok=False 时上游必 403，看这里就能定位。

    app_ver / ctx_getter 是客户端升级后最先失效的两样东西：
    版本号对不上或 getter 偏移变了，ctypes 通道要么 403 要么直接把进程崩掉。
    这里暴露出来，升级后一眼就能看出是不是又漂移了。

    selected / pure / last 是后端切换状态：
    selected 是配置值（auto/ctypes/pure/node），pure 表示纯 Python 通道可用，
    last 是各通道最近一次成败 —— ctypes 挂了但 pure 在撑，这里看得见。
    """
    try:
        st = uskey_cap.status()
    except Exception as e:          # 出票模块自己挂了也不能让 /health 500
        return {"ok": False, "backend": "", "error": str(e)[:120]}
    out = {"ok": bool(st.get("ok")), "backend": st.get("backend") or ""}
    for k in ("app_ver", "ctx_getter", "selected"):
        if st.get(k):
            out[k] = st[k]
    det = st.get("detail") or {}
    if det:
        out["channels"] = {k: bool(v) for k, v in det.items()}
    if st.get("pure_err"):
        out["pure_err"] = st["pure_err"][:120]
    last = st.get("last") or {}
    if last:
        out["last"] = last
    if not out["ok"]:
        out["error"] = "无可用通道：DLL、纯 Python、node 全不可用，将退回随机 uskey（上游 403）"
    return out


def cleanup(ctx):
    """清理过期产物与过期记录。手动接口和后台清理线程跑的是同一段代码。"""
    keep = int(ctx["config"].get("artifact_keep_days") or 30)
    cutoff = now() - keep * 86400
    files_dir = ctx["files_dir"]
    removed = 0
    if os.path.isdir(files_dir):
        for name in os.listdir(files_dir):
            p = os.path.join(files_dir, name)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
                    removed += 1
            except Exception:
                pass
    store = ctx["store"]
    usage_keep = int(ctx["config"].get("usage_keep_days") or 90)
    kept_usage = store.conn().execute(
        "SELECT COUNT(*) FROM usage WHERE at < ?", (now() - usage_keep * 86400,)).fetchone()[0]
    store.x("DELETE FROM usage WHERE at < ?", (now() - usage_keep * 86400,))
    # 过期的会话绑定也清掉（只增不减会一直涨，且老绑定会把人钉在低权重号上）
    ttl_days = float(ctx["config"].get("session_ttl_days") or 0)
    if ttl_days > 0:
        store.x("DELETE FROM sessions WHERE at < ?", (now() - ttl_days * 86400,))
    # tasks 只增不减，老记录一并清掉（只删已终态、且超过保留期的）
    task_keep = int(ctx["config"].get("task_keep_days") or 90)
    store.x("DELETE FROM tasks WHERE state IN ('completed','failed') AND created_at < ?",
            (now() - task_keep * 86400,))
    removed_tasks = store.conn().execute("SELECT changes()").fetchone()[0]
    result = {"ok": True, "removed_files": removed, "removed_tasks": removed_tasks,
              "removed_usage": kept_usage, "usage_keep_days": usage_keep,
              "keep_days": keep, "at": iso()}
    ctx["last_cleanup"] = result
    return result

def sanitize_info(ctx):
    """护栏现状：规则、策略、记忆条数、熔断。"""
    cfg = guard_cap.load_rules(ctx)
    rules = [{"id": r["id"], "where": r["where"], "pattern": r["rx"].pattern,
              "replace": r["replace"]} for r in cfg["rules"]]
    plans = []
    try:
        rows = ctx["store"].q(
            "SELECT fp, level, rule_ver, updated_at FROM sanitize_plan ORDER BY updated_at DESC LIMIT 50")
        plans = [{"fp": r["fp"], "level": r["level"], "rule_ver": r["rule_ver"],
                  "updated": iso(r["updated_at"])} for r in rows]
    except Exception:
        pass
    return {"ok": True, "path": cfg["path"], "rule_ver": cfg["mtime"],
            "policy": cfg["policy"], "rules": rules, "plans": plans,
            "stats": guard_cap.stats()}


def sanitize_reset(ctx):
    return guard_cap.reset_plans(ctx)


def _esc(v):
    """Prometheus 标签值转义：只处理反斜杠、引号、换行。"""
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _quantile(vals, q):
    if not vals:
        return 0
    idx = int(len(vals) * q)
    if idx >= len(vals):
        idx = len(vals) - 1
    return int(vals[idx])


def metrics_text(ctx, window=60):
    """Prometheus 文本格式（纯文本，curl | grep 就能读）。

    数据源只有 usage 表 + 内存瞬时值，不引入计数器，因此重启不归零。
    """
    window = max(5.0, min(float(window), 86400.0))
    since = now() - window
    out = []

    rows = ctx["store"].q(
        "SELECT model, status, COUNT(*) AS n,"
        " COALESCE(SUM(prompt_tokens),0) AS pt,"
        " COALESCE(SUM(completion_tokens),0) AS ct,"
        " COALESCE(SUM(reasoning_tokens),0) AS rt"
        " FROM usage WHERE at >= ? GROUP BY model, status ORDER BY model, status", (since,))
    for r in rows:
        m = _esc(r["model"] or "unknown")
        st = _esc(int(r["status"] or 0))
        out.append('marvis_requests_total{model="%s",status="%s"} %d' % (m, st, r["n"]))
        out.append('marvis_tokens_total{model="%s",kind="prompt"} %d' % (m, r["pt"]))
        out.append('marvis_tokens_total{model="%s",kind="completion"} %d' % (m, r["ct"]))
        out.append('marvis_tokens_total{model="%s",kind="reasoning"} %d' % (m, r["rt"]))

    for mrow in ctx["store"].q(
            "SELECT model FROM usage WHERE at >= ? GROUP BY model ORDER BY model", (since,)):
        m = _esc(mrow["model"] or "unknown")
        vals = [x["ms"] for x in ctx["store"].q(
            "SELECT ms FROM usage WHERE at >= ? AND model IS ? ORDER BY ms",
            (since, mrow["model"])) if x["ms"] is not None]
        for q in (0.5, 0.9, 0.99):
            out.append('marvis_duration_ms{model="%s",quantile="%s"} %d' % (m, q, _quantile(vals, q)))

    all_ms = [x["ms"] for x in ctx["store"].q(
        "SELECT ms FROM usage WHERE at >= ? ORDER BY ms", (since,)) if x["ms"] is not None]
    for q in (0.5, 0.9, 0.99):
        out.append('marvis_duration_ms{model="",quantile="%s"} %d' % (q, _quantile(all_ms, q)))

    try:
        out.append("marvis_inflight %d" % int(ctx["pool"].total_inflight()))
    except Exception:
        out.append("marvis_inflight 0")
    try:
        for k, v in sorted(ctx["pool"].counts().items()):
            if isinstance(v, int):
                out.append('marvis_accounts{state="%s"} %d' % (_esc(k), v))
    except Exception:
        pass
    for r in ctx["store"].q("SELECT state, COUNT(*) n FROM tasks GROUP BY state"):
        out.append('marvis_tasks{state="%s"} %d' % (_esc(r["state"]), r["n"]))

    started = ctx.get("started") or 0
    out.append("marvis_uptime_seconds %d" % (int(time.time() - started) if started else 0))
    out.append("marvis_usage_window_seconds %d" % int(window))
    return "\n".join(out) + "\n"
