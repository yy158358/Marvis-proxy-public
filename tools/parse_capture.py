#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓包文件解析器：把 .har / .saz / 纯文本 里的请求整理成可读清单。

用法：
    python3 tools/parse_capture.py capture/xxx.har
    python3 tools/parse_capture.py capture/xxx.saz
    python3 tools/parse_capture.py capture/          # 扫目录下所有

输出：按 URL 分组，打印方法/状态/请求头/请求体（JSON 自动格式化）/响应体开头。
     明文 token 会被打码（只留前 6 后 4）。
"""
import json
import os
import re
import sys
import zipfile
from urllib.parse import urlsplit


# ---------- 打码 ----------

SECRET_KEYS = ("authorization", "access-token", "accesstoken", "refresh-token",
               "refreshtoken", "cookie", "set-cookie", "bot_token", "x-api-key")


def mask(v):
    s = str(v)
    if len(s) <= 12:
        return "***"
    return s[:6] + "…" + s[-4:]


def mask_headers(h):
    out = []
    for k, v in h:
        low = k.lower()
        if any(s in low for s in SECRET_KEYS):
            v = mask(v)
        out.append((k, v))
    return out


def mask_body(text, limit=4000):
    t = text
    t = re.sub(r'(mv_[A-Za-z0-9]{10,})', lambda m: mask(m.group(1)), t)
    t = re.sub(r'("(?:accessToken|refreshToken|access_token|refresh_token|token|bot_token)"\s*:\s*")([^"]{8,})"',
               lambda m: m.group(1) + mask(m.group(2)) + '"', t)
    return t[:limit]


def pretty(body):
    if not body:
        return ""
    s = body if isinstance(body, str) else body.decode("utf-8", "replace")
    try:
        return json.dumps(json.loads(s), ensure_ascii=False, indent=2)
    except Exception:
        return s


# ---------- HAR ----------

def from_har(path):
    d = json.load(open(path, encoding="utf-8"))
    items = []
    for e in d.get("log", {}).get("entries", []):
        req, res = e.get("request", {}), e.get("response", {})
        hdrs = [(h.get("name", ""), h.get("value", "")) for h in req.get("headers", [])]
        rbody = req.get("postData", {}).get("text", "")
        rb = res.get("content", {}).get("text", "")
        items.append({
            "url": req.get("url", ""), "method": req.get("method", ""),
            "status": res.get("status", 0), "req_headers": hdrs,
            "req_body": rbody, "res_body": rb,
            "res_headers": [(h.get("name", ""), h.get("value", ""))
                            for h in res.get("headers", [])],
        })
    return items


# ---------- SAZ ----------

def from_saz(path):
    """Fiddler .saz = zip(raw/*.txt 原始报文, _index.htm 索引)。只取原始报文。"""
    items = []
    with zipfile.ZipFile(path) as z:
        names = sorted(n for n in z.namelist() if n.startswith("raw/") and n.endswith(".txt"))
        for n in names:
            raw = z.read(n).decode("utf-8", "replace")
            parts = raw.split("\r\n\r\n")
            head = parts[0]
            body = "\r\n\r\n".join(parts[1:])
            # 一个 .txt 里可能是「请求 + 响应」拼接，按 HTTP/ 行切
            chunks = re.split(r"(?m)^(?=HTTP/\d\.\d )", raw)
            req = next((c for c in chunks if c.startswith(("GET ", "POST ", "PUT ",
                                                           "DELETE ", "HEAD ", "OPTIONS "))), "")
            res = next((c for c in chunks if c.startswith("HTTP/")), "")
            url = ""
            m = re.match(r"(?m)^(?:GET|POST|PUT|DELETE|HEAD|OPTIONS)\s+(\S+)", req)
            if m:
                url = m.group(1)
                hm = re.search(r"(?im)^Host:\s*(\S+)", req)
                if hm and not url.startswith("http"):
                    url = "https://" + hm.group(1) + url
            status = 0
            sm = re.match(r"HTTP/\d\.\d\s+(\d{3})", res)
            if sm:
                status = int(sm.group(1))
            items.append({"url": url, "method": (req.split(" ")[0] if req else ""),
                          "status": status, "req_headers": [], "req_body": body,
                          "res_body": "\r\n\r\n".join(res.split("\r\n\r\n")[1:]),
                          "res_headers": [], "raw": raw[:0]})
    return items


# ---------- 输出 ----------

def dump(items, only_host=None):
    if only_host:
        items = [i for i in items if only_host in i["url"]]
    items.sort(key=lambda i: (urlsplit(i["url"]).netloc, urlsplit(i["url"]).path))
    print("共 %d 条\n" % len(items))
    last = None
    for i in items:
        sp = urlsplit(i["url"])
        key = sp.netloc
        if key != last:
            print("\n" + "=" * 70)
            print("HOST:", key)
            print("=" * 70)
            last = key
        print("\n%-6s %-58s → %s" % (i["method"], (sp.path + ("?" + sp.query if sp.query else ""))[:58], i["status"]))
        if i["req_headers"]:
            for k, v in mask_headers(i["req_headers"]):
                print("    > %s: %s" % (k, v))
        b = pretty(i["req_body"])
        if b:
            print("    REQ:")
            for ln in mask_body(b).splitlines():
                print("      " + ln)
        rb = pretty(i["res_body"])
        if rb:
            print("    RES:")
            for ln in mask_body(rb, 1200).splitlines()[:40]:
                print("      " + ln)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    path = sys.argv[1]
    only = sys.argv[2] if len(sys.argv) > 2 else None
    files = []
    if os.path.isdir(path):
        for n in sorted(os.listdir(path)):
            if n.lower().endswith((".har", ".saz", ".json")):
                files.append(os.path.join(path, n))
    else:
        files = [path]
    if not files:
        print("没找到 .har / .saz 文件")
        return
    for f in files:
        print("\n#### %s ####" % f)
        try:
            if f.lower().endswith(".saz"):
                items = from_saz(f)
            else:
                items = from_har(f)
        except Exception as e:
            print("解析失败:", type(e).__name__, e)
            continue
        dump(items, only)


if __name__ == "__main__":
    main()
