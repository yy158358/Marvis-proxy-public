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

#: 一键接入自动发的那把钥匙的前缀。
#:
#: 它和用户自己建的 mvk_ 是同一类东西（同样能鉴权、同样记用量），但：
#:   · 由服务在第一次接线时自己生成并存进库，用户不需要知道、也看不到；
#:   · 不给它开 enable/disable/delete —— 它是接线流程的一部分，删了下次接线又会长回来；
#:   · 前缀不同，所以配置里躺一把 mkx_ 时，看的人立刻知道「这是这台机器自动发的，
#:     不是谁手填进来的」，日志里也能区分。
WIRE_PREFIX = "mkx_"

#: client_keys.kind 的取值。
KIND_USER = "user"
KIND_WIRE = "wire"


def _h(token):
    return hashlib.sha256(token.encode()).hexdigest()


def enabled_count(ctx):
    return ctx["store"].q1("SELECT COUNT(*) AS n FROM client_keys WHERE enabled=1")["n"]


def lookup(ctx, token):
    """按明文查一把 key 的记录；查不到返回 None。

    只用于「这把我是不是见过」的提示（例如接线时把 key 写进 agent 配置之前先确认
    一下），**不做鉴权**。鉴权走 gate()。
    """
    if not token:
        return None
    return ctx["store"].q1("SELECT * FROM client_keys WHERE key=?", (_h(str(token)),))


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
        # 一键接入那把不列出来：用户看不见它，也就不会想去 enable/disable 它。
        # 它确实在库里、也在用（uses 会涨），只是不出现在这个界面。
        if _kind_of(r) == KIND_WIRE:
            continue
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


def _kind_of(row):
    """老库的行没有 kind 列（或为空串），一律当普通 key。"""
    if row is None:
        return KIND_USER
    return (row["kind"] if "kind" in row.keys() else "") or KIND_USER


def create_key(ctx, body):
    note = str((body or {}).get("note") or "").strip()[:80]
    token = PREFIX + secrets.token_hex(24)
    h = _h(token)
    ctx["store"].x("INSERT INTO client_keys(key,note,enabled,created_at,prefix,last_used_at,uses,kind)"
                   " VALUES(?,?,?,?,?,?,?,?)",
                   (h, note, 1, time.time(), token[:12], 0, 0, KIND_USER))
    return {"ok": True, "key": token, "id": h[:12], "note": note,
            "hint": "明文只显示这一次，请立刻保存；之后 /v1/* 需带 X-API-Key 或 Authorization: Bearer <key>"}


def _wire_row(ctx):
    """取那把自动发的接入钥匙（可能没有）。"""
    return ctx["store"].q1(
        "SELECT * FROM client_keys WHERE kind=? ORDER BY created_at LIMIT 1", (KIND_WIRE,))


def wire_key(ctx):
    """接线要写进 agent 配置的那把钥匙。没有就现发一把。

    为什么单独有这么一把，而不是让用户先建 key 再接线：
      用户刚装完 agent，想让它能用，这条路上不该出现「先去后台建一把 key 抄下来」
      这一站 —— 那是给「别的机器 / 别的客户端」准备的流程。接线口在本机、
      已经被 admin_token 或 loopback 守着，它替 agent 申请一把自己的钥匙是合理的。

    存明文，是因为每次接线都要把它写进配置文件；用户看不到它，所以也不存在
    「明文只显示一次」的问题。库是本地 SQLite，本来就有账号凭证。

    这把钥匙和普通 key 权限完全一样（都能对话、都能调 /v1/wire/*）：它就是一把
    正常的 key，只是来路和展示方式不同。这样「一键接入的那把」和「自己建的」
    在代码里没有任何分叉，不会出现「某功能只对其中一种生效」的意外。
    """
    row = _wire_row(ctx)
    if row is not None:
        # 存明文，取出来就是能直接用的 token（见上面的设计说明）
        plain = row["plain"] if "plain" in row.keys() else ""
        if plain:
            return plain
        # 老库或列缺失：重新生成，旧行删掉
        ctx["store"].x("DELETE FROM client_keys WHERE key=?", (row["key"],))
    token = WIRE_PREFIX + secrets.token_hex(24)
    ctx["store"].x("INSERT INTO client_keys(key,note,enabled,created_at,prefix,last_used_at,uses,kind,plain)"
                   " VALUES(?,?,?,?,?,?,?,?,?)",
                   (_h(token), "一键接入自动发放（用户不可见）", 1, time.time(),
                    token[:12], 0, 0, KIND_WIRE, token))
    return token


def key_action(ctx, kid, action):
    store = ctx["store"]
    rows = store.q("SELECT * FROM client_keys")
    target = [r for r in rows if r["key"].startswith(kid)]
    if not target:
        raise LookupError("找不到 key：%s" % kid)
    if len(target) > 1:
        raise ValueError("前缀 %s 匹配到 %d 个 key，请给更长的前缀" % (kid, len(target)))
    if _kind_of(target[0]) == KIND_WIRE:
        # 它不出现在 key list 里，正常也不会被点到；万一有人拿到前缀直接调，
        # 明确拒绝比「删了之后下一次接线又长出来」更好懂。
        raise ValueError("这是“一键接入”自动发的钥匙，不在列表里也不受管理："
                         "它跟着接线流程走，删了下次接线会自动重新发一把。"
                         "想让它失效，改 config.json 里的 admin_token 或换一台机器接。")
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
