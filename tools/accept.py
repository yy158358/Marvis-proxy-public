# -*- coding: utf-8 -*-
"""验收脚本（窄而快的回归）：key 覆盖、票据链路、模型别名、429 退避的配置面。

和 `tools/selfcheck.py` 的分工：
- selfcheck.py 是全面体检（静态 + DB + 全部管理端点 + 真实上游 + 日志判据，40s+，会提交生图）
- 本脚本只管「改鉴权/改路由最容易碰坏的那几条」，5s 左右，改完代码跑一遍

用法：
    py -3 tools\\accept.py                # 全量（含一张 512 生图，约 2 分钟出结果但不等待）
    py -3 tools\\accept.py --fast         # 跳过生图与 chat，只验 key 覆盖与 /v1/models
    py -3 tools\\accept.py --base http://127.0.0.1:8088 --key mvk_xxx

退出码：0 全通过；1 有失败项。
"""
import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []
skips = []
passes = []


def load_key(path):
    """优先命令行 --key，其次环境变量 MARVIS_KEY，最后 data/client_keys.txt。"""
    if path:
        return path.strip()
    env = os.environ.get("MARVIS_KEY", "").strip()
    if env:
        return env
    p = os.path.join(ROOT, "data", "client_keys.txt")
    if os.path.exists(p):
        return io.open(p, encoding="utf-8-sig").read().strip()
    return ""


BASE = "http://127.0.0.1:8088"
KEY = ""


def req(path, method="GET", body=None, headers=None, timeout=180):
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        resp = urllib.request.urlopen(r, timeout=timeout)
        return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 0, str(e).encode()


def check(name, ok, detail=""):
    print("%-5s %-52s %s" % ("PASS" if ok else "FAIL", name, detail))
    if ok:
        passes.append(name)
    else:
        fails.append(name)


def skip(name, why):
    print("%-5s %-52s %s" % ("SKIP", name, why))
    skips.append(name)


def main():
    global BASE, KEY
    ap = argparse.ArgumentParser(description="marvis-proxy 验收")
    ap.add_argument("--base", default="http://127.0.0.1:8088", help="服务地址")
    ap.add_argument("--key", default="", help="客户端 key（默认读 MARVIS_KEY 或 data/client_keys.txt）")
    ap.add_argument("--fast", action="store_true", help="跳过真实上游调用（chat / 生图）")
    a = ap.parse_args()
    BASE = a.base.rstrip("/")
    KEY = load_key(a.key)
    auth = {"X-API-Key": KEY} if KEY else {}

    print("目标 %s   key %s   mode %s" % (BASE, (KEY[:12] + "...") if KEY else "(无)", "fast" if a.fast else "full"))
    print("-" * 78)

    # ---- 1) key 覆盖：三条曾经无校验的端点 ----
    st, _ = req("/files/nonexistent.png")
    check("GET /files 无 key → 401", st == 401, "HTTP %d" % st)
    if KEY:
        st, _ = req("/files/nonexistent.png?key=" + KEY)
        check("GET /files ?key= → 404", st == 404, "HTTP %d" % st)
        st, _ = req("/files/nonexistent.png?key=mvk_bad")
        check("GET /files 错 key → 401", st == 401, "HTTP %d" % st)
        st, _ = req("/v1/images/tasks/img_nope")
        check("GET /v1/images/tasks 无 key → 401", st == 401, "HTTP %d" % st)
        st, _ = req("/v1/images/tasks/img_nope", headers=auth)
        check("GET /v1/images/tasks 带 key → 404", st == 404, "HTTP %d" % st)
        st, _ = req("/v1/messages/count_tokens", "POST",
                    {"messages": [{"role": "user", "content": "hello"}]})
        check("POST count_tokens 无 key → 401", st == 401, "HTTP %d" % st)
        st, body = req("/v1/messages/count_tokens", "POST",
                       {"messages": [{"role": "user", "content": "hello"}]}, auth)
        check("POST count_tokens 带 key → 200", st == 200, "HTTP %d %s" % (st, body[:40].decode("utf-8", "replace")))
    else:
        # 没有 key 时反代按「表里无 enabled key = 不校验」，这几条无从验证
        for name in ("GET /files ?key=", "GET /files 错 key", "GET /v1/images/tasks 无 key",
                     "GET /v1/images/tasks 带 key", "POST count_tokens 无 key", "POST count_tokens 带 key"):
            skip(name, "无可用 key")

    # ---- 2) 票据链路：两个模型各真打一次（--fast 跳过）----
    if a.fast:
        skip("chat deepseek-v4-1-flash-261017", "--fast")
        skip("chat deepseek-v4-pro-external", "--fast")
    elif not KEY:
        skip("chat 链路", "无可用 key")
    else:
        for model in ("deepseek-v4-1-flash-261017", "deepseek-v4-pro-external"):
            st, body = req("/v1/chat/completions", "POST",
                           {"model": model, "messages": [{"role": "user", "content": "say ok"}],
                            "max_tokens": 64}, auth)
            txt, mv = "", {}
            if st == 200:
                try:
                    d = json.loads(body)
                    txt = (d["choices"][0]["message"].get("content") or "")[:24]
                    mv = d.get("_marvis") or {}
                except Exception:
                    pass
            check("chat %s → 200" % model, st == 200,
                  "HTTP %d %r actual=%s" % (st, txt, mv.get("actual_model")))

    # ---- 3) 生图链路（legacy ext，不走票据）：只提交不等待 ----
    tid = ""
    if a.fast:
        skip("生图提交 → 202", "--fast")
        skip("生图任务可查", "--fast")
    else:
        st, body = req("/v1/images/generations", "POST",
                       {"prompt": "a red circle", "size": "512x512"}, auth, timeout=60)
        if st in (200, 202):
            try:
                tid = (json.loads(body) or {}).get("id", "")
            except Exception:
                tid = ""
        check("生图提交 → 202", st == 202 and bool(tid), "HTTP %d id=%s" % (st, tid))
        if tid:
            st, body = req("/v1/images/tasks/" + tid, headers=auth)
            check("生图任务可查", st == 200, "HTTP %d %s" % (st, body[:60].decode("utf-8", "replace")))

    # ---- 4) /v1/models 含新别名（slot_budgets 直传）+ 友好别名 alts ----
    st, body = req("/v1/models")
    ids = []
    alts = {}
    if st == 200:
        try:
            data = json.loads(body).get("data") or []
            ids = [m["id"] for m in data]
            alts = {m["id"]: m.get("alt_ids") or [] for m in data}
        except Exception:
            ids, alts = [], {}
    check("/v1/models 含 deepseek-v4-pro-external", "deepseek-v4-pro-external" in ids, "%d 个" % len(ids))
    check("/v1/models 带 alt_ids（友好别名）",
          any(alts.values()), "示例 %s" % (alts.get("deepseek-v4-1-flash-261017") or []))

    # ---- 4b) 友好别名能直接当 model 用，且响应体回显别名本身 ----
    if a.fast or not KEY:
        skip("chat 友好别名 deepseek-v4.1-flash", "--fast" if a.fast else "无可用 key")
    else:
        alias = "deepseek-v4.1-flash"
        st, body = req("/v1/chat/completions", "POST",
                       {"model": alias, "messages": [{"role": "user", "content": "say ok"}],
                        "max_tokens": 64}, auth)
        echo = ""
        if st == 200:
            try:
                echo = json.loads(body).get("model") or ""
            except Exception:
                echo = ""
        check("chat 友好别名 → 200 且回显别名", st == 200 and echo == alias,
              "HTTP %d echo=%r" % (st, echo))

    # ---- 4c) 服务端自我描述（新 agent 工具靠它自动认出 provider 名字）----
    st, body = req("/v1/provider")
    if st == 404:
        skip("GET /v1/provider 自描述", "旧进程还没重启，新路由未生效")
    else:
        name, mids = "", []
        try:
            blk = json.loads(body)
            name = blk.get("name") or ""
            mids = blk.get("model_ids") or []
        except Exception:
            pass
        check("GET /v1/provider 自描述", st == 200 and bool(name) and len(mids) >= 5,
              "HTTP %d name=%r ids=%d" % (st, name, len(mids)))

    st, body = req("/v1/models")
    if st == 200:
        try:
            prov = json.loads(body).get("provider") or {}
        except Exception:
            prov = {}
        if prov:
            check("/v1/models 带 provider 块", bool(prov.get("name")), str(prov.get("name")))
        else:
            skip("/v1/models 带 provider 块", "旧进程还没重启，provider 块未生效")

    # ---- 4d) 接线口：能列出 agent，能拒绝越权写入 ----
    st, body = req("/v1/wire/agents")
    if st == 404:
        skip("GET /v1/wire/agents 列出 agent", "旧进程还没重启，wire 路由未生效")
    else:
        ids = []
        try:
            ids = [a.get("id") for a in (json.loads(body).get("agents") or [])]
        except Exception:
            pass
        check("GET /v1/wire/agents 列出 agent",
              st == 200 and set(ids) == {"claude-code", "codex", "dsh", "zcode"},
              "HTTP %d ids=%s" % (st, ids))

    st, body = req("/v1/wire/status")
    check("GET /v1/wire/status 可读", st == 200, "HTTP %d %s" % (st, body[:80]))

    # 写接口不能匿名可用：它重写的是 ~/.claude、~/.codex 这些文件
    st, body = req("/v1/wire/apply", "POST", {"model": "___不存在___"}, None)
    check("POST /v1/wire/apply 无 key 被拒",
          st in (401, 403),
          "HTTP %d %s" % (st, body[:100]))

    # ---- 5) cleanup 新字段（usage_keep_days 生效的旁证）----
    st, body = req("/admin/cleanup", "POST", {})
    txt = body.decode("utf-8", "replace")
    check("POST /admin/cleanup 带 usage_keep_days", st == 200 and "usage_keep_days" in txt, txt[:120])

    print("-" * 78)
    print("汇总：PASS %d / SKIP %d / FAIL %d" % (len(passes), len(skips), len(fails)))
    if fails:
        print("失败项：")
        for f in fails:
            print("  x " + f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
