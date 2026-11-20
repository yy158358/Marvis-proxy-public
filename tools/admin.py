# -*- coding: utf-8 -*-
"""管理命令行：加号、API Key、账号池。省得手搓 curl。

覆盖 serve.py 里全部 /admin/* 路由，用法见 `admin.py help`。

  py -3 tools\\admin.py help
  py -3 tools\\admin.py key new --note 我的笔电
  py -3 tools\\admin.py key list
  py -3 tools\\admin.py login start --type QC
  py -3 tools\\admin.py login exchange --text "<整段落地页内容或 URL>"
  py -3 tools\\admin.py accounts
  py -3 tools\\admin.py account 3 probe
  py -3 tools\\admin.py usage

鉴权：admin_token 为空时本机免鉴权（默认如此，服务只听 127.0.0.1）。
配了 admin_token 就用 --token 或环境变量 MARVIS_ADMIN_TOKEN 带上。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("MARVIS_BASE") or "http://127.0.0.1:8088"
TOKEN = os.environ.get("MARVIS_ADMIN_TOKEN") or ""


def call(method, path, body=None, token=None):
    url = BASE.rstrip("/") + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    t = (token if token is not None else TOKEN).strip()
    if t:
        headers["X-Admin-Token"] = t
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, _parse(raw)
    except urllib.error.HTTPError as e:
        return e.code, _parse(e.read().decode("utf-8", "replace"))
    except urllib.error.URLError as e:
        return 0, {"error": "连不上 %s：%s（服务起了吗？先跑 run.bat）" % (url, e.reason)}


def _parse(raw):
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw}


def out(rc, obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return rc


# ---------------- 命令 ----------------
def cmd_help(a=None):
    print(__doc__.strip())
    return 0


def cmd_key(a):
    act = (a.arg1 or "").lower()
    kid = a.arg2 or ""
    if act == "new":
        rc, o = call("POST", "/admin/keys", {"note": a.note or ""})
        if o.get("ok"):
            print("=" * 68)
            print("  新 API Key（明文只显示这一次，立刻保存）：")
            print("  " + o["key"])
            print("=" * 68)
            print("  客户端填法：")
            print("    Base URL  http://127.0.0.1:8088/v1")
            print("    API Key   " + o["key"])
            print()
            return 0
        return out(rc, o)
    if act == "list":
        rc, o = call("GET", "/admin/keys")
        if not o.get("ok"):
            return out(rc, o)
        print("需要 key：%s" % ("是" if o.get("required") else "否（表里没有启用的 key，当前本机免鉴权）"))
        if not o.get("keys"):
            print("  （还没有任何 key）")
        for k in o.get("keys", []):
            print("  %-14s %-8s uses=%-4s %s" % (
                k["id"], "启用" if k["enabled"] else "停用", k.get("uses", 0), k.get("note") or ""))
        return 0
    if act in ("disable", "enable", "delete"):
        if not kid:
            print("用法：key %s <id 前缀>" % act, file=sys.stderr)
            return 2
        rc, o = call("POST", "/admin/keys/%s/%s" % (kid, act))
        if o.get("ok"):
            print("ok：%s 已 %s" % (kid, act))
            print("现在需要 key：%s" % ("是" if o.get("required") else "否"))
            return 0
        return out(rc, o)
    print("key 的动作：new | list | disable <id> | enable <id> | delete <id>", file=sys.stderr)
    return 2


def cmd_login(a):
    act = (a.arg1 or "").lower()
    if act == "start":
        rc, o = call("POST", "/admin/login/start", {"type": a.type})
        if not o.get("ok"):
            return out(rc, o)
        print("=" * 68)
        print("  在手机浏览器里打开这个链接，登录并授权：")
        print()
        print("  " + o["url"])
        print()
        print("  state : %s" % o["state"])
        print("  注意  : %s" % o.get("expires_note", ""))
        print("=" * 68)
        print("授权完成后，复制落地页的**整段内容**（JSON 正文优先，其次地址栏整条 URL），")
        print("然后执行：")
        print()
        print('  py -3 tools\\admin.py login exchange --text "<刚复制的内容>"')
        print()
        return 0
    if act == "exchange":
        text = a.text
        if not text:
            print("把落地页整段内容或地址栏 URL 用 --text 传进来", file=sys.stderr)
            return 2
        rc, o = call("POST", "/admin/login/exchange", {"text": text})
        if not o.get("ok"):
            return out(rc, o)
        print("加号成功：id=%s openid=%s token_len=%s refresh=%s"
              % (o.get("id"), o.get("openid"), o.get("token_len"), o.get("has_refresh")))
        pr = o.get("probe") or {}
        print("探测    ：%s" % json.dumps(pr, ensure_ascii=False))
        return 0
    print("login 的动作：start | exchange", file=sys.stderr)
    return 2


def cmd_accounts(a):
    rc, o = call("GET", "/admin/accounts")
    if not o.get("ok"):
        return out(rc, o)
    c = o.get("counts") or {}
    print("账号池：共 %s / 活跃 %s / 冷却 %s / 过期 %s"
          % (c.get("total"), c.get("active"), c.get("cooling"), c.get("expired")))
    for r in o.get("accounts", []):
        print("  #%-3s %-9s openid=%-10s ok=%-5s fail=%-3s inflight=%s"
              % (r.get("id"), r.get("status"), r.get("openid", ""),
                 r.get("ok_count", 0), r.get("fail_count", 0), r.get("inflight", 0)))
    return 0


def cmd_account(a):
    if not a.arg1 or not a.arg2:
        print("用法：account <id> <probe|refresh|disable|enable|delete>", file=sys.stderr)
        return 2
    rc, o = call("POST", "/admin/accounts/%s/%s" % (a.arg1, a.arg2))
    return out(rc, o)


def cmd_usage(a):
    rc, o = call("GET", "/admin/usage?limit=%s" % a.limit)
    if not o.get("ok"):
        return out(rc, o)
    for r in o.get("items", []):
        print("  %s  %-28s in=%-6s out=%-6s %s"
              % (r.get("at", ""), r.get("model", ""), r.get("prompt_tokens", 0),
                 r.get("completion_tokens", 0), r.get("status", "")))
    return 0


def cmd_health(a):
    rc, o = call("GET", "/health")
    if not o.get("ok"):
        return out(rc, o)
    u = o.get("uskey") or {}
    ch = u.get("channels") or {}
    print("服务    ：pid=%s %s:%s" % (o.get("pid"), (o.get("bind") or {}).get("host"),
                                     (o.get("bind") or {}).get("port")))
    print("账号    ：%s" % json.dumps(o.get("accounts") or {}, ensure_ascii=False))
    print("出票    ：backend=%s ctx=%s" % (u.get("backend"), u.get("ctx_getter")))
    print("通道    ：" + "  ".join("%s=%s" % (k, v) for k, v in ch.items()))
    print("模型    ：%s" % ", ".join(o.get("models") or []))
    return 0


COMMANDS = {
    "help": cmd_help,
    "key": cmd_key,
    "login": cmd_login,
    "accounts": cmd_accounts,
    "account": cmd_account,
    "usage": cmd_usage,
    "health": cmd_health,
}


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("cmd", nargs="?", default="help")
    ap.add_argument("arg1", nargs="?")
    ap.add_argument("arg2", nargs="?")
    ap.add_argument("--note", default="")
    ap.add_argument("--type", default="QC", help="登录类型 QC / WX")
    ap.add_argument("--text", default="")
    ap.add_argument("--limit", default="20")
    ap.add_argument("--token", default=None)
    ap.add_argument("--base", default=None)
    a = ap.parse_args()

    global BASE, TOKEN
    if a.base:
        BASE = a.base
    if a.token is not None:
        TOKEN = a.token

    fn = COMMANDS.get(a.cmd)
    if fn is None:
        print("未知命令：%s" % a.cmd, file=sys.stderr)
        cmd_help()
        return 2
    return fn(a)


if __name__ == "__main__":
    sys.exit(main())
