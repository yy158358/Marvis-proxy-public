# -*- coding: utf-8 -*-
"""Claude Code：``~/.claude/settings.json``。

Claude Code 没有「供应商」这个概念：它只认 ``env.ANTHROPIC_BASE_URL`` 指向谁、用
``env.ANTHROPIC_AUTH_TOKEN`` 认证。所以网关地址落在 env 里，而不是某个 provider 块里。

两个和通用工具不一样的地方（因为我们在网关里，能拿到真实事实）：

1. ``ANTHROPIC_BASE_URL`` 用**不带 /v1** 的根地址。Claude Code 自己拼 ``/v1/messages``，
   写 ``.../v1`` 会变成 ``/v1/v1/messages``。marvis-proxy 的 ``/v1/provider`` 给的是带
   ``/v1`` 的 base_url，所以这里必须自己还原。
2. ``CLAUDE_CODE_MAX_CONTEXT_TOKENS`` / ``CLAUDE_CODE_MAX_OUTPUT_TOKENS`` 从
   ``models.json`` 的 ``context_window`` / ``max_output_tokens`` 取。写死一个数字会让
   Claude Code 在真正该压缩的时候不压缩（或反过来提前压缩）。

``/model`` 还能用的前提是 ``env.ANTHROPIC_MODEL`` **不在**：它压过选择器，会让每一轮都
钉在同一个模型上。所以接线期间我们删掉它，并记下它原来在不在、是什么值。
"""
from __future__ import annotations

import json

from .base import Adapter, Entry
from .edit import detect_json_style, json_dump, json_get, json_load

SETTINGS = ".claude/settings.json"

BASE_URL = "env.ANTHROPIC_BASE_URL"
AUTH_TOKEN = "env.ANTHROPIC_AUTH_TOKEN"
MODEL = "model"
SMALL_FAST = "env.ANTHROPIC_SMALL_FAST_MODEL"
OUTPUT_TOKENS = "env.CLAUDE_CODE_MAX_OUTPUT_TOKENS"
CONTEXT_TOKENS = "env.CLAUDE_CODE_MAX_CONTEXT_TOKENS"
API_KEY_HELPER = "env.ANTHROPIC_API_KEY"

#: 档位 -> settings 键。Claude Code 的 opus/sonnet/haiku 选择项经这些键解析，
#: 所以接好它们，``/model`` 才仍然有意义。
TIERS = {
    "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "fable": "ANTHROPIC_DEFAULT_FABLE_MODEL",
}
TIER_KEYS = tuple("env." + key for key in TIERS.values())
#: ANTHROPIC_MODEL 压过 /model，接线期间必须不在；ANTHROPIC_API_KEY 同理，Claude Code
#: 见到它就走 sk-ant- 那条直连路径，绕过我们。两个都要记原值才能回滚。
REMOVED_WHILE_WIRED = ("env.ANTHROPIC_MODEL", "env.ANTHROPIC_API_KEY")

OWNED = (BASE_URL, AUTH_TOKEN, MODEL, SMALL_FAST, OUTPUT_TOKENS, CONTEXT_TOKENS) + TIER_KEYS

#: 回滚需要、但设置文件里不存在的两条记账 entry；前导 "__" 保证撞不上真实键。
_TOP = "__top"
_ENV = "__env"
_STYLE = "__style"


class _Ref(object):
    __slots__ = ("data", "compact")

    def __init__(self, text):
        self.data = json_load(text)
        self.compact = bool(text.strip()) and "\n" not in text.strip()


def _key_order(data, name):
    target = data if name == "" else data.get(name)
    if not isinstance(target, dict):
        return []
    return list(target)


def _reorder(obj, order):
    known = [k for k in order if k in obj]
    extra = [k for k in obj if k not in order]
    rebuilt = {k: obj[k] for k in (*known, *extra)}
    obj.clear()
    obj.update(rebuilt)


class ClaudeCodeAdapter(Adapter):

    agent_id = "claude-code"
    display_name = "Claude Code"
    files = (SETTINGS,)
    needs_restart = False
    shadows_env_keys = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL")

    # ---- hooks -----------------------------------------------------------
    def plan(self, model, effort):
        path = self.path(SETTINGS)
        text, _, _ = self.load_text(path)
        ref = _Ref(text)
        entries = []
        for dotted in OWNED + REMOVED_WHILE_WIRED:
            value = json_get(ref.data, dotted)
            entries.append(Entry(file=str(path), path=dotted.split("."),
                                 present=value is not None, value=value, kind="json"))
        # 键序也是文件内容的一部分：只还原值不还原位置，用户会得到一个没请我们改的
        # diff。把允许我们重排的两个对象（顶层、env）的原始键序记下来。
        for name, marker in (("", _TOP), ("env", _ENV)):
            order = _key_order(ref.data, name)
            if order:
                entries.append(Entry(file=str(path), path=[marker], present=True,
                                     value=json.dumps(order, ensure_ascii=False),
                                     kind="json-order"))
        entries.append(Entry(file=str(path), path=[_STYLE], present=True,
                             value=json.dumps(detect_json_style(text)), kind="json-style"))
        # 档位推导警告是「这次 apply 的结论」，清掉上一次的。
        self._render_warnings = []
        return entries

    def render(self, path, text, newline, entries, model, effort):
        facts = self.facts
        target = facts.require(model)
        data = json_load(text)
        style = detect_json_style(text)
        env = data.get("env")
        if not isinstance(env, dict):
            env = {}
            data["env"] = env

        env["ANTHROPIC_BASE_URL"] = facts.anthropic_url
        env["ANTHROPIC_AUTH_TOKEN"] = self.gateway_key
        # 别留着 ANTHROPIC_API_KEY：Claude Code 见到它会优先走 sk-ant- 那条路。
        env.pop("ANTHROPIC_API_KEY", None)
        data["model"] = target.id

        # `ANTHROPIC_MODEL` 压过 /model，接线中的会话不能带。
        env.pop("ANTHROPIC_MODEL", None)

        tiers, warnings = facts.tier_models()
        self._render_warnings = warnings
        for tier, key in TIERS.items():
            if tier in tiers:
                env[key] = tiers[tier]
        env["ANTHROPIC_SMALL_FAST_MODEL"] = tiers.get("haiku") or target.id

        if target.context_window:
            env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] = str(target.context_window)
        if target.max_output_tokens:
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(target.max_output_tokens)
        return json_dump(data, style)

    def unrender(self, path, text, newline, entries):
        style_entry = next((e for e in entries if e.kind == "json-style"), None)
        style = json.loads(style_entry.value) if style_entry is not None else detect_json_style(text)
        data = json_load(text)
        for entry in entries:
            if entry.kind in ("json-order", "json-style"):
                continue
            if entry.present:
                _json_put(data, _entry_path(entry), entry.value)
            else:
                _json_drop(data, _entry_path(entry))
        for entry in entries:
            if entry.kind != "json-order":
                continue
            target = data if entry.path == [_TOP] else data.get("env")
            order = json.loads(entry.value)
            if isinstance(target, dict):
                _reorder(target, order)
        return json_dump(data, style)

    def extra_warnings(self):
        warnings = list(getattr(self, "_render_warnings", []))
        self._render_warnings = []
        path = self.path(SETTINGS)
        text, _, _ = self.load_text(path)
        if text.strip():
            try:
                json_load(text)
            except ValueError as exc:
                warnings.append("%s 不是合法 JSON（%s），不会动它" % (path, exc))
        return warnings


def _entry_path(entry):
    """记录下来的键路径（旧快照可能存的是点号字符串）。"""
    raw = entry.path
    if isinstance(raw, str):
        head, _, tail = raw.partition(".")
        return ["env", tail] if head == "env" and tail else [raw]
    return [str(p) for p in raw]


def _json_put(data, path, value):
    cur = data
    for part in path[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[path[-1]] = value


def _json_drop(data, path):
    chain = [data]
    for part in path[:-1]:
        nxt = chain[-1].get(part)
        if not isinstance(nxt, dict):
            return
        chain.append(nxt)
    chain[-1].pop(path[-1], None)
    for i in range(len(chain) - 1, 0, -1):
        if chain[i]:
            break
        chain[i - 1].pop(path[i - 1], None)
