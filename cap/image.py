# -*- coding: utf-8 -*-
"""文生图（A2A / vedas.image）：提交 → 轮询 GetTask → 下载产物。

实测要点：
  · 提交是 JSON-RPC SendStreamingMessage，SSE 里先给 result.task{id, contextId, TASK_STATE_SUBMITTED}
  · 轮询用同一端点 method=GetTask，params {"id": task_id}；不校验 Marvis-Conversation-ID
  · 产物 url 自带 ?version=N，绝不能自拼 version=1
  · 典型耗时 60–110s，失败样本约 210–420s（云端沙箱内部失败，任务照常受理）
  · MarvisExt 可选（实测去掉照样成功），由 config.send_marvis_ext 控制
"""
import base64

from cap import device as device_cap
import json
import urllib.error
import urllib.request
import uuid

BASE = "https://yybadaccess.3g.qq.com"
MEDIA = BASE + "/marvis-vedas-image"
RPC = MEDIA + "/rpc"


def new_conv():
    return "conv_" + uuid.uuid4().hex


def marvis_ext(guid):
    """兼容旧调用：真正的实现在 cap/device.py（uskey 每请求新随机，不能缓存）。"""
    return device_cap.marvis_ext(guid, profile="legacy")


def headers(account, config, conv_id, accept="text/event-stream", with_content_type=True):
    h = {
        "Accept": accept,
        "A2A-Version": "1.0",
        "User-Agent": config.get("user_agent") or "python-urllib/3",
        "Ual-Access-Openid": account["openid"],
        "Ual-Access-Access-Token": account["access_token"],
        "Ual-Access-Login-Type": str(1 if (account["login_type"] or "QC") == "QC" else 2),
        "Ual-Access-Guid": account["guid"] or "",
        "Marvis-AgentTag": "file-agent",
        "Marvis-Conversation-ID": conv_id,
        "Marvis-Scene": "tool:dispatch_task",
        "Marvis-Protocol-Version": "1000",
        "Ual-Access-Requestid": "resp_" + uuid.uuid4().hex,
    }
    if with_content_type:
        h["Content-Type"] = "application/json"
    if config.get("send_marvis_ext"):
        h["Ual-Access-MarvisExt"] = marvis_ext(account["guid"] or "")
    return h


def submit_payload(prompt, size):
    text = ("全局任务目标为：%s\n当前任务目标为：%s，尺寸 %dx%d，只生成 1 张，格式 PNG。"
            % (prompt, prompt, size, size))
    return json.dumps({
        "method": "SendStreamingMessage",
        "params": {
            "message": {
                "messageId": "msg_" + uuid.uuid4().hex[:11] + "_" + uuid.uuid4().hex[:12],
                "contextId": "sa_" + uuid.uuid4().hex[:11] + "_" + uuid.uuid4().hex[:10],
                "role": "ROLE_USER",
                "parts": [{"text": text}],
            },
            "configuration": {},
            "metadata": {"capabilityId": "vedas.image"},
        },
        "id": str(uuid.uuid4()), "jsonrpc": "2.0",
    }, ensure_ascii=False).encode()


def submit(account, config, prompt, size, conv_id, on_event=None, timeout=120):
    """提交任务，读 SSE 直到拿到 task id（或流结束）。

    返回 {"ok":True,"task_id","context_id","stage"} 或 {"ok":False,"error","status"}。
    401 等 HTTP 错误也走 ok=False，由调用方决定是否扣分。
    """
    req = urllib.request.Request(RPC, data=submit_payload(prompt, size),
                                 headers=headers(account, config, conv_id), method="POST")
    task_id = ""
    context_id = ""
    stage = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if not chunk or chunk == "[DONE]":
                    continue
                try:
                    obj = json.loads(chunk)
                except ValueError:
                    continue
                res = obj.get("result") or {}
                task = res.get("task") or {}
                if task.get("id") and not task_id:
                    task_id = task["id"]
                    context_id = task.get("contextId") or ""
                    if on_event:
                        on_event("submitted", {"task_id": task_id, "context_id": context_id})
                su = res.get("statusUpdate")
                if su:
                    title = ""
                    stages = ((su.get("metadata") or {}).get("stages") or [])
                    if stages:
                        title = stages[0].get("title") or ""
                    if title and title != stage:
                        stage = title
                        if on_event:
                            on_event("stage", {"title": title,
                                               "state": (su.get("status") or {}).get("state")})
                if task_id and stage:
                    break
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "error": (e.read() or b"")[:300].decode("utf-8", "replace")}
    except Exception as e:
        if not task_id:
            return {"ok": False, "status": 0, "error": "%s: %s" % (type(e).__name__, e)}
    if not task_id:
        return {"ok": False, "status": 0, "error": "SSE 里没拿到 task id"}
    return {"ok": True, "task_id": task_id, "context_id": context_id, "stage": stage}


def get_task(account, config, task_id, conv_id="conv_probe", timeout=30):
    """查任务状态。返回 {"ok":True,"state","artifacts","stage","error"}。"""
    body = json.dumps({"jsonrpc": "2.0", "id": "poll", "method": "GetTask",
                       "params": {"id": task_id}}).encode()
    req = urllib.request.Request(RPC, data=body, method="POST",
                                 headers=headers(account, config, conv_id, accept="application/json"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            obj = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "state": "", "error": (e.read() or b"")[:200].decode("utf-8", "replace")}
    except Exception as e:
        return {"ok": False, "status": 0, "state": "", "error": "%s: %s" % (type(e).__name__, e)}
    res = obj.get("result") or {}
    status = res.get("status") or {}
    state = status.get("state") or ""
    text = "".join(p.get("text", "") for p in ((status.get("message") or {}).get("parts") or []))
    stage = ""
    stages = ((res.get("metadata") or {}).get("stages") or [])
    if stages:
        stage = stages[-1].get("title") or ""
    return {"ok": True, "state": state, "artifacts": res.get("artifacts") or [],
            "stage": stage, "error": text}


def artifact_url(art):
    for p in art.get("parts") or []:
        if p.get("url"):
            return p["url"]
    return ""


def absolute(url):
    if not url:
        return ""
    if url.startswith("http"):
        return url
    return MEDIA + url


def download(account, config, url, conv_id, timeout=300):
    target = absolute(url)
    h = headers(account, config, conv_id, accept="*/*", with_content_type=False)
    req = urllib.request.Request(target, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get("Content-Type") or ""


def sniff_mime(head, fallback="image/png"):
    """按文件头判 MIME，不信任 Content-Type。"""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", ".gif"
    return fallback, ".png"
