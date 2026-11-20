# -*- coding: utf-8 -*-
"""客户端 API Key：让 /v1/* 从「本机谁都能用」变成「凭 key 用」。

设计取舍：
  · 库里只存 sha256，不存明文；明文只在创建那一次返回。
  · 表里没有任何 enabled key 时 = 不校验（保持现在的零配置行为）。
  · 同一把 key 同时充当粘性身份的种子（sha256 前 16 位），换 key 就等于换会话身份。
"""
import hashlib
import secrets
import time

PREFIX = "mvk_"


def _h(token):
    return hashlib.sha256(token.encode()).hexdigest()


def enabled_count(ctx):
    return ctx["store"].q1("SELECT COUNT(*) AS n FROM client_keys WHERE enabled=1")["n"]


def gate(ctx, headers):
    """返回 (ok, identity)。identity 是粘性种子；空串 = 未提供身份。"""
    token = (headers.get("X-API-Key") or "").strip()
    if not token:
        auth = headers.get("Authorization") or ""
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else auth.strip()
    if not token:
        return (enabled_count(ctx) == 0, "")
    h = _h(token)
    row = ctx["store"].q1("SELECT * FROM client_keys WHERE key=?", (h,))
    if row is None:
        # 表为空时允许任意字符串（老行为），方便先用起来
        return (enabled_count(ctx) == 0, h[:16])
    if not row["enabled"]:
        return (False, h[:16])
    try:
        ctx["store"].x("UPDATE client_keys SET last_used_at=?, uses=uses+1 WHERE key=?",
                       (time.time(), h))
    except Exception:
        pass
    return (True, h[:16])


def list_keys(ctx):
    rows = ctx["store"].q("SELECT * FROM client_keys ORDER BY created_at DESC")
    out = []
    for r in rows:
        out.append({
            "id": r["key"][:12],
            "prefix": r["prefix"] if "prefix" in r.keys() else "",
            "note": r["note"] or "",
            "enabled": bool(r["enabled"]),
            "created": r["created_at"],
            "last_used": r["last_used_at"] if "last_used_at" in r.keys() else 0,
            "uses": r["uses"] if "uses" in r.keys() else 0,
        })
    return {"ok": True, "required": enabled_count(ctx) > 0, "keys": out}


def create_key(ctx, body):
    note = str((body or {}).get("note") or "").strip()[:80]
    token = PREFIX + secrets.token_hex(24)
    h = _h(token)
    ctx["store"].x("INSERT INTO client_keys(key,note,enabled,created_at,prefix,last_used_at,uses)"
                   " VALUES(?,?,?,?,?,?,?)",
                   (h, note, 1, time.time(), token[:12], 0, 0))
    return {"ok": True, "key": token, "id": h[:12], "note": note,
            "hint": "明文只显示这一次，请立刻保存；之后 /v1/* 需带 X-API-Key 或 Authorization: Bearer <key>"}


def key_action(ctx, kid, action):
    store = ctx["store"]
    rows = store.q("SELECT * FROM client_keys")
    target = [r for r in rows if r["key"].startswith(kid)]
    if not target:
        raise LookupError("找不到 key：%s" % kid)
    if len(target) > 1:
        raise ValueError("前缀 %s 匹配到 %d 个 key，请给更长的前缀" % (kid, len(target)))
    k = target[0]["key"]
    if action == "disable":
        store.x("UPDATE client_keys SET enabled=0 WHERE key=?", (k,))
    elif action == "enable":
        store.x("UPDATE client_keys SET enabled=1 WHERE key=?", (k,))
    elif action == "delete":
        store.x("DELETE FROM client_keys WHERE key=?", (k,))
    else:
        raise ValueError("action 只能是 enable/disable/delete")
    return {"ok": True, "id": k[:12], "action": action, "required": enabled_count(ctx) > 0}
