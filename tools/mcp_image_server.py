#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Marvis 生图 MCP 服务（stdio，JSON-RPC 2.0）。

## 它解决什么问题

模型不该为了画一张图先去探测端口、读源码、再手搓 curl 轮询。这个服务把
「提交 → 轮询 → 落盘 → 返回本地路径」封成一个工具，模型看到的就是：

    generate_image(prompt, size="1024")

## 协议

遵循 MCP stdio 传输：每行一条 JSON-RPC 2.0 消息（不是 LSP 的 Content-Length 头）。
支持 `initialize` / `tools/list` / `tools/call` / `ping`，其余方法返回 -32601。

## 依赖与配置

纯标准库，不需要 pip。配置按优先级取：

1. 环境变量 `MARVIS_PROXY_BASE`（默认 http://127.0.0.1:8088）
2. 环境变量 `MARVIS_PROXY_KEY`（代理的客户端 key）
3. 否则从 `<repo>/data/client_keys.txt` 里取第一个非注释行

没有 key 而代理又要求鉴权时，工具会明确返回错误而不是静默失败。

## 为什么用 wait 轮询而不是只返回 task id

MCP 工具调用是同步语义，模型拿到 task id 还得自己再调一次工具才能取图，
等于把复杂度又推回给模型。这里在服务端等完（默认 480 秒上限），直接给文件路径。

用法（DSH 侧配置见 README §11.13）：

    python tools/mcp_image_server.py
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_NAME = "marvis-image"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"

DEFAULT_BASE = os.environ.get("MARVIS_PROXY_BASE") or "http://127.0.0.1:8088"
DEFAULT_TIMEOUT = 540           # 服务端 image_timeout 是 480，留一点余量


def log(msg):
    # stdout 是协议通道，日志一律走 stderr
    sys.stderr.write("[mcp-image] %s\n" % msg)
    sys.stderr.flush()


def read_key():
    k = (os.environ.get("MARVIS_PROXY_KEY") or "").strip()
    if k:
        return k
    path = os.path.join(ROOT, "data", "client_keys.txt")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line.split()[0]
    except OSError:
        pass
    return ""


def http_json(method, path, body=None, key="", base=None, timeout=60):
    url = (base or DEFAULT_BASE).rstrip("/") + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if key:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw.decode("utf-8"))
            except ValueError:
                return r.status, {"_raw": raw[:500].decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw.decode("utf-8"))
        except ValueError:
            return e.code, {"_raw": raw[:500].decode("utf-8", "replace")}
    except Exception as e:                                  # 连接失败等
        return 0, {"error": str(e)}


TOOL_SPEC = {
    "name": "generate_image",
    "description": (
        "用 Marvis 生图能力按文字描述生成一张图片（文生图，PNG）。"
        "服务端已完成提交与轮询，返回图片的本地绝对路径，可直接读取或展示。"
        "提示：512 档约 30~60 秒，768 档约 3~4 分钟，1024 档约 1~2 分钟；"
        "1280 档上游不支持，会直接报错。一次只画一张，需要多张就多次调用。"
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "画面描述，中文即可；上游会自行改写提示词"},
            "size": {"type": "integer", "enum": [512, 768, 1024],
                     "default": 1024, "description": "正方形边长像素，只支持这三档"},
            "model": {"type": "string", "default": "marvis-image",
                      "description": "生图模型名，一般不用改"},
        },
        "required": ["prompt"],
    },
}


def tool_generate(args):
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return {"isError": True, "content": [{"type": "text", "text": "prompt 不能为空"}]}
    size = int(args.get("size") or 1024)
    model = args.get("model") or "marvis-image"
    key = read_key()
    base = args.get("base") or DEFAULT_BASE

    deadline = time.time() + float(args.get("timeout") or DEFAULT_TIMEOUT)
    st, obj = http_json("POST", "/v1/images/generations",
                        {"model": model, "prompt": prompt, "size": size},
                        key=key, base=base, timeout=120)
    if st == 401:
        return {"isError": True, "content": [{"type": "text", "text":
                "代理要求客户端 key（401）。请设置 MARVIS_PROXY_KEY，"
                "或把 key 写进 data/client_keys.txt。"}]}
    if st not in (200, 202):
        return {"isError": True, "content": [{"type": "text", "text":
                "提交失败 HTTP %s：%s" % (st, json.dumps(obj, ensure_ascii=False)[:400])}]}

    task_id = obj.get("id") or ""
    status_url = obj.get("status_url") or ("/v1/images/tasks/" + task_id)
    status = obj.get("status") or ""
    if not task_id:
        return {"isError": True, "content": [{"type": "text", "text":
                "服务端没返回任务 id：%s" % json.dumps(obj, ensure_ascii=False)[:400]}]}

    # 已经是同步结果（wait=true 或 n>1 的批次）就直接取
    files = _files_of(obj)
    if not files:
        delay = 3.0
        while time.time() < deadline:
            time.sleep(delay)
            delay = min(delay + 1.0, 6.0)
            st, obj = http_json("GET", status_url, key=key, base=base, timeout=60)
            if st != 200:
                continue
            status = obj.get("status") or ""
            files = _files_of(obj)
            if files:
                break
            if status in ("failed", "error", "cancelled"):
                return {"isError": True, "content": [{"type": "text", "text":
                        "生图失败（%s）：%s" % (status, obj.get("error") or "上游返回失败，可能是该尺寸档位超时")}]}
        else:
            return {"isError": True, "content": [{"type": "text", "text":
                    "等待超时（%ds），任务 %s 仍在 %s。稍后可用 status_url 复查：%s"
                    % (int(DEFAULT_TIMEOUT), task_id, status or "running", status_url)}]}

    local = []
    for f in files:
        url = f.get("url") or ""
        if url.startswith("/files/"):
            local.append(os.path.join(ROOT, "data", "files", os.path.basename(url)))
        elif url:
            local.append(url)
    paths = [p for p in local if p]
    text = "已生成 %d 张，本地路径：\n%s" % (len(paths), "\n".join(paths)) if paths \
        else "任务完成但没有产物：%s" % json.dumps(obj, ensure_ascii=False)[:400]
    return {"isError": False, "content": [{"type": "text", "text": text}]}


def _files_of(obj):
    out = []
    for d in (obj.get("data") or []):
        if isinstance(d, dict):
            if d.get("url"):
                out.append({"url": d["url"]})
            elif d.get("b64_json"):
                out.append(d)
    for it in (obj.get("items") or []):                 # n>1 的批次形态
        for d in (it.get("data") or []):
            if isinstance(d, dict) and d.get("url"):
                out.append({"url": d["url"]})
    return out


# ------------------------------------------------------------------ JSON-RPC

def reply(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle(msg):
    method = msg.get("method") or ""
    mid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return reply(mid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "notifications/initialized" or mid is None:
        return None                                      # 通知不需要回包
    if method == "ping":
        return reply(mid, {})
    if method == "tools/list":
        return reply(mid, {"tools": [TOOL_SPEC]})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name != "generate_image":
            return error(mid, -32602, "未知工具：%s" % name)
        try:
            return reply(mid, tool_generate(args))
        except Exception as e:
            log("tools/call 异常：%r" % (e,))
            return reply(mid, {"isError": True,
                               "content": [{"type": "text", "text": "内部错误：%s" % e}]})
    return error(mid, -32601, "method not found: %s" % method)


def main():
    log("启动（base=%s，key=%s）" % (DEFAULT_BASE, "有" if read_key() else "无"))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out = error(None, -32700, "parse error")
        else:
            out = handle(msg)
            if out is None:
                continue
        try:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except Exception as e:
            log("写回失败：%r" % (e,))
            return 1
    return 0


if __name__ == "__main__":
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main() or 0)
