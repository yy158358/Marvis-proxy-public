# -*- coding: utf-8 -*-
"""Anthropic Messages 适配层：让 Claude Code 这类只说 Anthropic 协议的客户端能用。

映射要点：
  · claude-haiku* → flash 槽位（Claude Code 拿 haiku 跑后台小任务，缺档会 400）
  · claude-sonnet* / claude-opus* → main-auto
  · tools / tool_use / tool_result 与 OpenAI function 互转
  · 流式：OpenAI chunk → Anthropic 的 message_start / content_block_* / message_delta / message_stop
"""
import json
import time
import uuid

from api import openai as O
from cap import chat as chat_cap

STOP_MAP = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use",
            "content_filter": "end_turn", None: "end_turn"}


# ---------------- 模型 ----------------
def resolve_claude(models, name):
    name = (name or "").strip()
    al = models.get("claude_aliases") or []
    for a in al:
        if a["name"] == name:
            return a["slot"], name
    for a in al:
        if name.startswith(a["name"]):
            return a["slot"], name
    low = name.lower()
    if "haiku" in low:
        return "deepseek-v4-flash-external", name
    if "claude" in low:
        return "main-auto", name
    # 也允许直接用 marvis-* 别名或槽位名
    return O.resolve_slot(models, name)


# ---------------- 请求转换 ----------------
def _content_text(blocks):
    out = []
    for b in blocks or []:
        if b.get("type") == "text":
            out.append(b.get("text") or "")
    return "".join(out)


def to_openai(body):
    """Anthropic 请求体 → (messages, tools, kwargs)。"""
    msgs = []
    system = body.get("system")
    if isinstance(system, str) and system.strip():
        msgs.append({"role": "system", "content": system})
    elif isinstance(system, list):
        txt = _content_text(system)
        if txt.strip():
            msgs.append({"role": "system", "content": txt})

    for m in body.get("messages") or []:
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, str):
            msgs.append({"role": role, "content": content})
            continue
        blocks = content or []
        texts, images, tool_calls, tool_results = [], [], [], []
        for b in blocks:
            t = b.get("type")
            if t == "text":
                texts.append(b.get("text") or "")
            elif t == "tool_use":
                tool_calls.append({
                    "id": b.get("id") or "call_" + uuid.uuid4().hex[:20],
                    "type": "function",
                    "function": {"name": b.get("name") or "",
                                 "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)},
                })
            elif t == "tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    c = _content_text(c)
                tool_results.append({"role": "tool",
                                     "tool_call_id": b.get("tool_use_id") or "",
                                     "content": c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)})
            elif t == "image":
                src = b.get("source") or {}
                if src.get("type") == "base64":
                    url = "data:%s;base64,%s" % (src.get("media_type") or "image/png", src.get("data") or "")
                    images.append({"type": "image_url", "image_url": {"url": url}})

        if role == "assistant":
            msg = {"role": "assistant", "content": "".join(texts) if texts else None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            msgs.append(msg)
        else:
            msgs.extend(tool_results)          # 工具结果必须先紧跟 assistant.tool_calls
            if texts or images:
                if images:
                    parts = [{"type": "text", "text": "".join(texts)}] if texts else []
                    parts.extend(images)
                    msgs.append({"role": "user", "content": parts})
                else:
                    msgs.append({"role": "user", "content": "".join(texts)})

    tools = None
    if body.get("tools"):
        tools = []
        for t in body["tools"]:
            tools.append({"type": "function", "function": {
                "name": t.get("name"), "description": t.get("description") or "",
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}})
    return msgs, tools


def tool_choice(body):
    tc = body.get("tool_choice") or {}
    t = tc.get("type")
    if t == "auto":
        return "auto"
    if t == "any":
        return "required"
    if t == "tool" and tc.get("name"):
        return {"type": "function", "function": {"name": tc["name"]}}
    if tc.get("disable_parallel_tool_use"):
        return "auto"
    return None


def _effort_from_thinking(body):
    """Anthropic 的 thinking 块 → 上游的 reasoning_effort。返回 None 表示客户端没表态。

    Anthropic 的 extended thinking 和 OpenAI 的 reasoning_effort 是同一件事的两套写法。
    不翻译过去，上游就不知道要分离思考，会把整段推理写进正文（见 api/openai.py 的
    _normalize_effort 里那段实测记录）。这里只做翻译，「没表态时至少给 low」的兜底
    交给 chat() 里统一收口，不在这里重复第二套规则。
    """
    th = body.get("thinking")
    if th is True:                      # 有的客户端直接写 thinking: true
        return "medium"
    if not isinstance(th, dict):
        return None
    if th.get("type") not in (None, "enabled"):
        return None                     # type == "disabled"：交给兜底
    budget = th.get("budget_tokens")
    if isinstance(budget, int) and budget > 0:
        if budget >= 8192:
            return "high"
        if budget >= 2048:
            return "medium"
        return "low"
    return "medium"                     # 只说 enabled、不给预算


def build_kwargs(body):
    kw = {}
    for k_src, k_dst in (("max_tokens", "max_tokens"), ("temperature", "temperature"),
                         ("top_p", "top_p")):
        if body.get(k_src) is not None:
            kw[k_dst] = body[k_src]
    if body.get("stop_sequences"):
        kw["stop"] = body["stop_sequences"]
    eff = _effort_from_thinking(body)
    if eff:
        kw["reasoning_effort"] = eff
    return kw


# ---------------- 响应转换 ----------------
def to_anthropic(obj, model_name, input_tokens=0, with_thinking=False):
    """OpenAI completion dict → Anthropic message dict。"""
    choice = (obj.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    blocks = []
    # 只有客户端显式要了 extended thinking 才回 thinking 块，
    # 否则有的客户端（含 Claude Code 的部分版本）会因为未知块类型报错
    if with_thinking and msg.get("reasoning_content"):
        blocks.append({"type": "thinking", "thinking": msg["reasoning_content"], "signature": ""})
    if msg.get("content"):
        blocks.append({"type": "text", "text": msg["content"]})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        raw = fn.get("arguments") or "{}"
        try:
            inp = json.loads(raw)
        except Exception:
            inp = {"_raw": raw}
        blocks.append({"type": "tool_use", "id": tc.get("id") or ("toolu_" + uuid.uuid4().hex[:20]),
                       "name": fn.get("name") or "", "input": inp})
    if not blocks:
        blocks = [{"type": "text", "text": ""}]
    usage = obj.get("usage") or {}
    return {
        "id": "msg_" + uuid.uuid4().hex[:20],
        "type": "message",
        "role": "assistant",
        "model": model_name,
        "content": blocks,
        "stop_reason": STOP_MAP.get(choice.get("finish_reason"), "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or input_tokens or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
            "cache_read_input_tokens": int(((usage.get("prompt_tokens_details") or {})
                                            .get("cache_read_tokens")) or 0),
        },
    }


def estimate_tokens(body):
    """粗略估算 input_tokens：实测中文约 2 字符 = 1 token，英文约 4 字符 = 1 token。"""
    total_chars = 0
    system = body.get("system")
    if isinstance(system, str):
        total_chars += len(system)
    elif isinstance(system, list):
        total_chars += len(_content_text(system))
    for m in body.get("messages") or []:
        c = m.get("content")
        if isinstance(c, str):
            total_chars += len(c)
        elif isinstance(c, list):
            for b in c:
                if b.get("type") == "text":
                    total_chars += len(b.get("text") or "")
                elif b.get("type") == "tool_result":
                    cc = b.get("content")
                    total_chars += len(cc) if isinstance(cc, str) else len(_content_text(cc))
                elif b.get("type") == "tool_use":
                    total_chars += len(json.dumps(b.get("input") or {}, ensure_ascii=False))
    for t in body.get("tools") or []:
        total_chars += len(json.dumps(t, ensure_ascii=False))
    return max(1, int(total_chars / 2))


# ---------------- 入口 ----------------
def messages(ctx, body, client_key="", t0=None):
    slot, name = resolve_claude(ctx["models"], body.get("model"))
    msgs, tools = to_openai(body)
    kw = build_kwargs(body)
    tc = tool_choice(body)
    if tc:
        kw["tool_choice"] = tc
    if tools:
        kw["tools"] = tools
    want_stream = bool(body.get("stream"))
    if not kw.get("max_tokens"):
        kw["max_tokens"] = 4096

    oai = {"model": slot, "messages": msgs, "stream": want_stream}
    oai.update(kw)
    result = O.chat(ctx, oai, client_key)
    est = estimate_tokens(body)

    if result[0] == "json":
        _, _name, _slot, acc, raw, hdrs, ms, raised, retried, why = result
        obj = O.build_json_response(ctx, name, _slot, acc, raw, hdrs, ms, raised, retried, why)
        return ("json", to_anthropic(obj, name, est, with_thinking=bool(body.get("thinking"))))

    _, _name, _slot, acc, resp, meta = result
    return ("stream", stream_events(ctx, name, acc, resp, est, _slot, t0,
                                    with_thinking=bool(body.get("thinking"))))


def stream_events(ctx, name, acc, resp, input_tokens=0, slot="", t0=None, with_thinking=False):
    """把 OpenAI chunk 流翻译成 Anthropic 事件流（yield 完整 SSE 文本块）。"""

    def ev(event, data):
        return "event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False))

    mid = "msg_" + uuid.uuid4().hex[:20]
    yield ev("message_start", {"type": "message_start", "message": {
        "id": mid, "type": "message", "role": "assistant", "model": name, "content": [],
        "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": 0}}})

    text_open = False
    # 客户端要了 extended thinking 就把 index 0 留给 thinking 块，正文往后挪一格；
    # 没要就保持原样（thinking 内容照样丢弃，免得客户端认不出块类型报错）
    think_open = False
    THINK_INDEX = 0
    text_index = 1 if with_thinking else 0
    tool_state = {}          # oai index -> {"block": i, "id", "name", "pending_args", "opened"}
    next_index = text_index + 1
    stop_reason = None
    usage = {}
    chars = 0
    t0 = t0 or time.time()
    finished = False

    try:
        for kind, data in chat_cap.iter_sse(resp):
            if kind in ("ping", "other", "timeout", "error"):
                continue
            if kind == "done":
                break
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if obj.get("usage"):
                usage = obj["usage"]
            for ch in obj.get("choices") or []:
                d = ch.get("delta") or {}
                if ch.get("finish_reason"):
                    stop_reason = STOP_MAP.get(ch["finish_reason"], "end_turn")
                # 上游分离出来的思考：客户端要了 extended thinking 就发 thinking_delta（永远在正文之前）
                rt = d.get("reasoning_content")
                if rt and with_thinking:
                    if not think_open:
                        yield ev("content_block_start", {"type": "content_block_start", "index": THINK_INDEX,
                                                         "content_block": {"type": "thinking", "thinking": "",
                                                                           "signature": ""}})
                        think_open = True
                    yield ev("content_block_delta", {"type": "content_block_delta", "index": THINK_INDEX,
                                                     "delta": {"type": "thinking_delta", "thinking": rt}})
                text = d.get("content")
                if text:
                    if think_open:      # 正文开始了，思考块先收尾
                        yield ev("content_block_stop", {"type": "content_block_stop", "index": THINK_INDEX})
                        think_open = False
                    chars += len(text)
                    if not text_open:
                        yield ev("content_block_start", {"type": "content_block_start", "index": text_index,
                                                         "content_block": {"type": "text", "text": ""}})
                        text_open = True
                    yield ev("content_block_delta", {"type": "content_block_delta", "index": text_index,
                                                     "delta": {"type": "text_delta", "text": text}})
                for tc in d.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    st = tool_state.get(idx)
                    fn = tc.get("function") or {}
                    if st is None:
                        st = {"block": None, "id": "", "name": "", "args": "", "opened": False}
                        tool_state[idx] = st
                    if tc.get("id"):
                        st["id"] = tc["id"]
                    if fn.get("name"):
                        st["name"] += fn["name"]
                    if fn.get("arguments"):
                        st["args"] += fn["arguments"]
                    if not st["opened"] and st["name"]:
                        if think_open:
                            yield ev("content_block_stop", {"type": "content_block_stop", "index": THINK_INDEX})
                            think_open = False
                        if text_open:
                            yield ev("content_block_stop", {"type": "content_block_stop", "index": text_index})
                            text_open = False
                        st["block"] = next_index
                        next_index += 1
                        st["opened"] = True
                        yield ev("content_block_start", {
                            "type": "content_block_start", "index": st["block"],
                            "content_block": {"type": "tool_use",
                                              "id": st["id"] or "toolu_" + uuid.uuid4().hex[:20],
                                              "name": st["name"], "input": {}}})
                    if st["opened"] and fn.get("arguments"):
                        yield ev("content_block_delta", {
                            "type": "content_block_delta", "index": st["block"],
                            "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]}})

        if think_open:
            yield ev("content_block_stop", {"type": "content_block_stop", "index": THINK_INDEX})
            think_open = False
        if text_open:
            yield ev("content_block_stop", {"type": "content_block_stop", "index": text_index})
        for idx in sorted(tool_state):
            st = tool_state[idx]
            if not st["opened"]:
                # 只给了 id/name 没给 arguments 的极端情况
                st["block"] = next_index
                next_index += 1
                st["opened"] = True
                yield ev("content_block_start", {"type": "content_block_start", "index": st["block"],
                                                 "content_block": {"type": "tool_use",
                                                                   "id": st["id"] or "toolu_" + uuid.uuid4().hex[:20],
                                                                   "name": st["name"], "input": {}}})
            yield ev("content_block_stop", {"type": "content_block_stop", "index": st["block"]})

        if tool_state and stop_reason in (None, "end_turn"):
            stop_reason = "tool_use"
        ctx["pool"].on_success(acc["id"])
        yield ev("message_delta", {"type": "message_delta",
                                   "delta": {"stop_reason": stop_reason or "end_turn", "stop_sequence": None},
                                   "usage": {"output_tokens": int(usage.get("completion_tokens") or 0)}})
        yield ev("message_stop", {"type": "message_stop"})
        finished = True
    finally:
        try:
            O._record(ctx, "", name, slot, acc,
                      {"completion_tokens": chars // 2, "total_tokens": chars // 2},
                      time.time() - t0, True, 200 if finished else 0)
        except Exception:
            pass
        try:
            resp.close()
        except Exception:
            pass
        try:
            ctx["pool"].release(acc["id"])
        except Exception:
            pass


def count_tokens(body):
    return {"input_tokens": estimate_tokens(body)}
