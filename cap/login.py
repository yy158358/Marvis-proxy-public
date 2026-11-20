# -*- coding: utf-8 -*-
"""登录 / 续期：只做三件事——出授权链接、拿 code 兑换、refresh 换新 token。

实测（2026-11-17）：真实 code 兑换 → HTTP 302 + 8 条 Set-Cookie
（accesstoken/appid/expires_in/is_locked/logintype/openid/refreshtoken/scope）。
注意：**code 一次性**——同一个 code 第二次兑换回 200 但 body 是
{"code":-107,"msg":"RC_NO_AUTH_ERR"}，所以「浏览器先跳、我们再兑换」只有第一次能成功。
"""
import json
import time
import random
import hashlib
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://yybadaccess.3g.qq.com"
AUTHORIZE = "https://graph.qq.com/oauth2.0/authorize"
OAUTH_PATH = "/marvis_client_login/marvis_oauth"
REFRESH_PATH = "/marvis_client_login/marvis_refresh_token"
QQ_APP_ID = "1903794758"
BUSINESS_ID = "marvis_client"
ACCESS_KEY = "fpzkeTlcGLVP37qiOWd29MyaOqaJYxMi"
WX_APP_ID = "wxb650708317d56f88"
WX_AUTHORIZE = "https://open.weixin.qq.com/connect/qrconnect"
LOGIN_TYPE = {"QC": 1, "WX": 2}
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随 302：兑换结果的凭证在 Set-Cookie 里，跟走就丢了。"""

    def redirect_request(self, *a, **k):
        return None


def redirect_uri(login_type="QC"):
    return BASE + OAUTH_PATH + "?login_type=" + login_type


def authorize_url(state, login_type="QC"):
    if login_type == "WX":
        # 微信开放平台扫码登录：同机浏览器里通常要求「用微信打开」；另一台设备可直接扫码。
        q = [
            ("appid", WX_APP_ID),
            ("redirect_uri", redirect_uri("WX")),
            ("response_type", "code"),
            ("scope", "snsapi_login"),
            ("state", state),
        ]
        return WX_AUTHORIZE + "?" + urllib.parse.urlencode(q) + "#wechat_redirect"
    q = [
        ("response_type", "code"),
        ("client_id", QQ_APP_ID),
        ("redirect_uri", redirect_uri(login_type)),
        ("state", state),
        ("scope", "snsapi_login"),
    ]
    return AUTHORIZE + "?" + urllib.parse.urlencode(q)


def parse_callback(text):
    """从用户回填的内容里取 (code, state)。兼容整条 URL / 纯 query / 裸 code。"""
    text = (text or "").strip().strip("'\"")
    if not text:
        return "", ""
    if "code=" in text:
        qs = text.split("?", 1)[1] if "?" in text else text
        qs = qs.split("#", 1)[0]
        q = urllib.parse.parse_qs(qs)
        return (q.get("code") or [""])[0], (q.get("state") or [""])[0]
    return text, ""


def _from_set_cookie(headers):
    jar = {}
    for line in headers.get_all("Set-Cookie") or []:
        first = line.split(";", 1)[0]
        if "=" in first:
            k, v = first.split("=", 1)
            jar[k.strip()] = v.strip()
    if not jar.get("accesstoken") or not jar.get("openid"):
        return None
    return {
        "openid": jar.get("openid", ""),
        "access_token": jar.get("accesstoken", ""),
        "refresh_token": jar.get("refreshtoken", ""),
        "appid": jar.get("appid", ""),
        "scope": jar.get("scope", ""),
        "login_type": "QC" if jar.get("logintype", "1") == "1" else "WX",
        "expires_in": int(jar.get("expires_in") or 0),
        "is_locked": jar.get("is_locked", ""),
    }


def _normalize(info, fallback_openid=""):
    """上游 user_info 形态 → 我们的统一形态。

    fallback_openid：刷新接口的响应里 open_id 是空串（实测 2026-11-17），
    而 openid 我们本来就知道，用它补齐。
    """
    if not isinstance(info, dict):
        return None
    token = info.get("access_token") or info.get("accessToken")
    openid = (info.get("openid") or info.get("openId") or info.get("open_id")
              or fallback_openid)
    if not token or not openid:
        return None
    return {
        "openid": openid,
        "access_token": token,
        "refresh_token": info.get("refresh_token") or info.get("refreshToken") or "",
        "appid": info.get("appid", ""),
        "scope": info.get("scope", ""),
        "expires_in": int(info.get("expires_in") or info.get("expiresIn") or 0),
    }


def exchange(code, state="", guid="", login_type="QC", timeout=30):
    """用 code 换凭证。成功 {"ok":True,"info":{...},"status":302}；失败带上游 code/msg。"""
    q = {"login_type": login_type, "code": code}
    if state:
        q["state"] = state
    url = BASE + OAUTH_PATH + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "User-Agent": BROWSER_UA,
        "Ual-Access-Guid": str(guid or ""),
    })
    op = urllib.request.build_opener(_NoRedirect)
    try:
        with op.open(req, timeout=timeout) as r:
            status, headers, raw = r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        status, headers, raw = e.code, e.headers, e.read()
    except Exception as e:
        return {"ok": False, "error": "请求异常：%s" % e, "status": 0}

    body = (raw or b"").decode("utf-8", "replace")
    info = _from_set_cookie(headers)
    code_err, msg = None, ""
    if not info:
        try:
            obj = json.loads(body)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            info = _normalize(obj.get("user_info") or obj.get("userInfo") or {})
            if not info:
                code_err = obj.get("code")
                msg = obj.get("msg") or obj.get("message") or ""
    if not info:
        return {"ok": False, "error": "no_credentials", "status": status,
                "code": code_err, "msg": msg, "body_head": body[:200]}
    return {"ok": True, "info": info, "status": status}


def refresh_token(account, timeout=30):
    """用 refresh_token 换新三件套。account 需要 openid/access_token/refresh_token。

    注意：refresh_token 成对轮换，并发两次会让它失效——调用方必须单飞（pool.lock_refresh）。
    """
    payload = {"userInfo": {
        "openId": account["openid"],
        "refreshToken": account["refresh_token"],
        "accessToken": account["access_token"],
        "loginType": "Marvis",
    }}
    # 刷新接口是「业务接口」，必须带 md5 签名头：
    #   sig = md5(body + ts(毫秒) + ACCESS_KEY + nonce)
    # 少了这四头上游直接 400 空响应（2026-11-17 实测）。
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ts = str(int(time.time() * 1000))
    nonce = str(1000 + random.randint(0, 8999))
    sig = hashlib.md5(body + ts.encode() + ACCESS_KEY.encode() + nonce.encode()).hexdigest()
    req = urllib.request.Request(
        BASE + REFRESH_PATH,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "*/*",
                 "User-Agent": BROWSER_UA,
                 "Ual-Access-Businessid": BUSINESS_ID,
                 "Ual-Access-Timestamp": ts,
                 "Ual-Access-Nonce": nonce,
                 "Ual-Access-Signature": sig,
                 "Ual-Access-Openid": account["openid"],
                 "Ual-Access-Access-Token": account["access_token"],
                 "Ual-Access-Login-Type": str(LOGIN_TYPE.get(account.get("login_type") or "QC", 1)),
                 "Ual-Access-Guid": account.get("guid") or ""},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    except Exception as e:
        return {"ok": False, "error": "请求异常：%s" % e, "status": 0}
    try:
        obj = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {"ok": False, "error": "响应不是 JSON", "status": status,
                "body_head": (raw or b"")[:200].decode("utf-8", "replace")}
    info = _normalize(obj.get("user_info") or obj.get("userInfo") or obj,
                      fallback_openid=account["openid"])
    if not info:
        return {"ok": False, "error": "no_credentials", "status": status,
                "code": obj.get("code"), "msg": obj.get("msg", "")}
    return {"ok": True, "info": info, "status": status}
