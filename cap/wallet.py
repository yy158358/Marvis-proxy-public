# -*- coding: utf-8 -*-
"""额度（token 余额）：/v3/marvis_get_wallet，需要业务签名头。

签名 = md5(body + timestamp_ms + ACCESS_KEY + nonce)，ACCESS_KEY 来自客户端常量。
"""
import hashlib
import json
import random
import time
import urllib.error
import urllib.request

BASE = "https://yybadaccess.3g.qq.com"
WALLET_PATH = "/v3/marvis_get_wallet"
ACCESS_KEY = "fpzkeTlcGLVP37qiOWd29MyaOqaJYxMi"
BUSINESS_ID = "marvis_client"

_CACHE = {"at": 0.0, "key": "", "data": None}


def headers(account, body, config):
    ts = str(int(time.time() * 1000))
    nonce = str(1000 + random.randint(0, 8999))
    sig = hashlib.md5((body + ts + ACCESS_KEY + nonce).encode("utf-8")).hexdigest()
    return {
        "Accept": "*/*",
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": config.get("user_agent") or "Marvis/1.0",
        "Ual-Access-Businessid": BUSINESS_ID,
        "Ual-Access-Timestamp": ts,
        "Ual-Access-Nonce": nonce,
        "Ual-Access-Signature": sig,
        "Ual-Access-Guid": account.get("guid") or "",
        "Ual-Access-Access-Token": account.get("access_token") or "",
        "Ual-Access-Login-Type": str(account.get("login_type") or 1),
        "Ual-Access-Openid": account.get("openid") or "",
    }


def fetch(account, config, timeout=25):
    body = "{}"
    req = urllib.request.Request(BASE + WALLET_PATH, data=body.encode(),
                                 headers=headers(account, body, config), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code,
                "error": e.read().decode("utf-8", "replace")[:300]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    try:
        data = json.loads(raw)
    except Exception:
        return {"ok": False, "status": status, "error": "响应不是 JSON: " + raw[:200]}
    if data.get("code") != 0:
        return {"ok": False, "status": status, "code": data.get("code"), "error": data.get("msg")}
    d = data.get("data") or {}

    def num(k):
        try:
            return int(float(d.get(k) or 0))
        except Exception:
            return 0

    total, used = num("total_tokens"), num("used_tokens")
    return {"ok": True, "status": status,
            "total": total, "used": used, "avail": num("avail_tokens"), "frozen": num("frozen_tokens"),
            "percent": round(used * 100.0 / total, 1) if total else None,
            "wallet_status": d.get("status")}


def snapshot(ctx, ttl=60.0, force=False):
    """60 秒缓存：额度接口偶尔要 10 秒，不该被 UI 反复打。"""
    pool = ctx["pool"]
    acc = None
    for row in pool.all():
        if row["status"] == "active":
            acc = dict(row)
            break
    if acc is None:
        return {"ok": False, "error": "没有可用账号"}
    key = str(acc["id"])
    if not force and _CACHE["data"] and _CACHE["key"] == key and time.time() - _CACHE["at"] < ttl:
        return dict(_CACHE["data"], cached=True, age=round(time.time() - _CACHE["at"], 1))
    data = fetch(acc, ctx["config"])
    if data.get("ok"):
        _CACHE.update({"at": time.time(), "key": key, "data": data})
    return dict(data, cached=False, account_id=acc["id"])
