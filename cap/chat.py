# -*- coding: utf-8 -*-
"""对话上游：POST /v1/chat/completions（OpenAI 形状，槽位名当 model）。

实测要点：
  · 鉴权只要 4 个 Ual-Access-* 头（Openid / Access-Token / Login-Type / Guid），UA 任意
  · 请求 stream:false 时上游也可能回 SSE 文本（Content-Type: text/plain），必须按响应判断形态
  · 响应头 Marvis-Actual-Model / Marvis-Routed-Model / Marvis-Experiment-Id / X-Trace-Id 值得回显
"""
import json
import re
import socket
import time
import urllib.error
import urllib.request
import uuid

from cap import device as device_cap
from cap import guard as guard_cap
from cap import uskey as uskey_cap

BASE = "https://yybadaccess.3g.qq.com"
CHAT = BASE + "/v1/chat/completions"

# 只透传这些字段；其余静默丢弃（D8）
PASS_KEYS = ("max_tokens", "max_completion_tokens", "temperature", "top_p", "stop",
             "presence_penalty", "frequency_penalty", "response_format", "tools",
             "tool_choice", "parallel_tool_calls", "seed", "logprobs", "top_logprobs",
             "n", "user", "reasoning_effort")



def _clean_tools(tools):
    """上游网关会拒掉空 parameters 的工具（403 code 4100406 非法请求）。

    这里补齐成合法的空对象 schema；同时把 None 的 description 去掉，
    避免上游把 null 当非法字段。
    """
    if not isinstance(tools, list):
        return tools
    out = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function")
        if isinstance(fn, dict):
            fn = dict(fn)
            params = fn.get("parameters")
            if not isinstance(params, dict) or not params:
                fn["parameters"] = {"type": "object", "properties": {}}
            elif not params.get("type"):
                params = dict(params)
                params["type"] = "object"
                fn["parameters"] = params
            if fn.get("description") is None:
                fn.pop("description", None)
            t = dict(t)
            t["function"] = fn
        out.append(t)
    return out


def request_meta(slot, scene="agent_chat", tag="main"):
    """一次上游调用的上下文（uskey 票据要按它算，见 cap/uskey.py）。"""
    return {"model": slot or "main-auto", "scene": scene, "tag": tag,
            "nonce": str(uuid.uuid4()),
            "conv": "conv_" + uuid.uuid4().hex,
            "resp": "resp_" + uuid.uuid4().hex}


def headers(account, config, ext=None, meta=None):
    """ext=None 走配置（默认发）；True/False 强制。缺 MarvisExt 会被上游 403 4100404 风控。

    meta 给定时按客户端真实头集发，并用 Beacon SDK 现算的 uskey 出票。
    """
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": config.get("user_agent") or "python-urllib/3",
        "Ual-Access-Openid": account["openid"],
        "Ual-Access-Access-Token": account["access_token"],
        "Ual-Access-Login-Type": str(1 if (account["login_type"] or "QC") == "QC" else 2),
        "Ual-Access-Guid": account["guid"] or "",
    }
    guid = account["guid"] or ""
    # 对话链路的 Login-Type 客户端用的是 6（PC 客户端），与加号时的 QC/WX 不是一个维度
    if meta is not None:
        h["Ual-Access-Login-Type"] = str(config.get("marvis_login_type") or 6)
    bus_info = ""
    if meta is not None:
        bus_info = uskey_cap.bus_info(meta.get("tag") or "main", meta.get("conv") or "",
                                      meta.get("resp") or "", meta.get("scene") or "agent_chat",
                                      meta.get("model") or "main-auto", meta.get("nonce") or "",
                                      account["openid"])
    if device_cap.wanted(config, ext):
        h["Ual-Access-MarvisExt"] = device_cap.marvis_ext(
            guid, config=config, bus_info=bus_info, openid=account["openid"],
            login_type=h["Ual-Access-Login-Type"],
            nonce=(meta or {}).get("nonce"))
    if meta is not None:
        h["Marvis-AgentTag"] = meta.get("tag") or "main"
        h["Marvis-Conversation-ID"] = meta.get("conv") or ""
        h["Marvis-Response-ID"] = meta.get("resp") or ""
        h["Marvis-Scene"] = meta.get("scene") or "agent_chat"
        h["X-Device-GUID"] = guid
        h["Marvis-Protocol-Version"] = str(config.get("marvis_protocol_version") or 1000)
        h["Ual-Access-Requestid"] = str(uuid.uuid4())
    return h


def body(slot, messages, stream=False, level=0, ctx=None, **kw):
    """level 是护栏等级（0 只跑规则、1 注 U+2060、2 加前缀、3 system 降级）。"""
    messages, _rep = guard_cap.apply(messages, level, ctx)
    out = {"model": slot, "messages": messages, "stream": bool(stream)}
    for k in PASS_KEYS:
        if k in kw and kw[k] is not None:
            out[k] = kw[k]
    if "tools" in out:
        out["tools"] = _clean_tools(out["tools"])
    return out


class _ErrResp(object):
    """把 HTTPError 包装成「像 response 一样」的对象，方便上游 4xx/5xx 走正常分支。"""

    def __init__(self, err):
        self._err = err
        self.status = err.code
        self.headers = dict(err.headers.items())
        self.reason = getattr(err, "reason", "")

    def read(self, n=None):
        try:
            return self._err.read() if n is None else self._err.read(n)
        except Exception:
            return b""

    def readline(self):
        try:
            return self._err.readline()
        except Exception:
            return b""

    def close(self):
        try:
            self._err.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def open_request(account, config, payload, timeout=None, ext=None, meta=None):
    t = timeout or config.get("http_timeout") or 180
    req = urllib.request.Request(
        CHAT, data=json.dumps(payload).encode(),
        headers=headers(account, config, ext, meta=meta), method="POST")
    try:
        return urllib.request.urlopen(req, timeout=t)
    except urllib.error.HTTPError as e:
        # 上游 4xx/5xx：不抛，交给上层按状态码分类（403/401/429 都要能看到原文）
        return _ErrResp(e)


def call(account, config, payload, timeout=None, ext=None, meta=None):
    """非流式：返回 (status, headers, raw_bytes, ms)。HTTPError 也当正常返回。"""
    t0 = time.time()
    try:
        with open_request(account, config, payload, timeout, ext, meta=meta) as r:
            status, hdrs, raw = r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        status, hdrs, raw = e.code, e.headers, e.read()
    return status, hdrs, raw, int((time.time() - t0) * 1000)


def _apply_idle_timeout(resp, seconds):
    """给流式响应底层 socket 设读超时：上游卡住不吐数据时按 idle 判死。

    readline 在 socket 超时上抛 socket.timeout。不同 Python 版本的
    http.client 内部结构不同，取不到就静默退回「无 idle 超时」。
    """
    if not seconds or float(seconds) <= 0:
        return False
    try:
        resp.fp.raw._sock.settimeout(float(seconds))
        return True
    except Exception:
        return False


def stream(account, config, payload, timeout=None, ext=None, meta=None):
    """流式：返回未关闭的 response，调用方逐行读。

    sse_idle_timeout（默认 180s）：两块数据之间超过这个时间判上游卡死，
    iter_sse 会吐一个 timeout 事件并结束，避免请求线程被永久占住。
    """
    resp = open_request(account, config, payload, timeout, ext, meta=meta)
    _apply_idle_timeout(resp, config.get("sse_idle_timeout") or 0)
    return resp


def is_sse(headers, raw_prefix=b""):
    ctype = (headers.get("Content-Type") or "").lower()
    if "event-stream" in ctype:
        return True
    return raw_prefix.lstrip().startswith(b"data:")


def iter_sse(resp):
    """逐行吐 (kind, payload)。kind: data / done / ping / other / timeout / error。"""
    while True:
        try:
            line = resp.readline()
        except socket.timeout:
            yield "timeout", ""
            break
        except Exception:
            yield "error", ""
            break
        if not line:
            break
        text = line.decode("utf-8", "replace").strip()
        if not text:
            continue
        if text.startswith(":"):
            yield "ping", text[1:].strip()
        elif text.startswith("data:"):
            data = text[5:].strip()
            if data == "[DONE]":
                yield "done", ""
                break
            yield "data", data
        else:
            yield "other", text


def probe(account, config, timeout=30):
    """探活：max_tokens=1 打 main-auto。"""
    t0 = time.time()
    payload = {"model": "main-auto", "messages": [{"role": "user", "content": "1"}],
               "max_tokens": 1, "stream": False}
    status, hdrs, raw = 0, None, b""
    try:
        status, hdrs, raw = 0, None, b""
        status, hdrs, raw, ms = call(account, config, payload, timeout)
    except Exception as e:
        return {"ok": False, "status": 0, "ms": int((time.time() - t0) * 1000), "msg": str(e)}
    code = msg = ""
    if status != 200:
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
            code, msg = obj.get("code"), obj.get("msg")
        except Exception:
            msg = (raw or b"")[:160].decode("utf-8", "replace")
    return {"ok": status == 200, "status": status, "ms": ms,
            "actual_model": (hdrs.get("Marvis-Actual-Model") if hdrs else ""),
            "code": code, "msg": msg}
