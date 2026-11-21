# -*- coding: utf-8 -*-
"""ZCode（智谱的 agent harness）。

桌面端和 CLI 共用一个 provider 文件：``~/.zcode/v2/provider_config.json``（下划线，
``ZCODE_PERSONAL_PROVIDER_CONFIG_FILE`` 可以改位置）。它是 JSON，所以这里只按路径接
写而不整份重排：用户其它的 provider、顺序、模型覆盖都要字节不动。

两处刻意避开的坑：

* 旧工具写的是 ``~/.zcode/v2/config.json``。官方 schema 说两个前端共用
  ``provider_config.json``；已有 ``config.json`` 时一并改，但绝不新建。
* ``credentials.json`` 按设备加密，所以网关 key 放进 provider 规则的
  ``access.apiKey`` —— ZCode 没有环境变量间接的方式。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .base import Adapter, Entry
from .atomic import newline_of, to_newline

PROVIDER_CONFIG = ".zcode/v2/provider_config.json"
DESKTOP_CONFIG = ".zcode/v2/config.json"

#: 在 ZCode 里注册的供应商 id，与网关的 provider_id 一致；换这个值等于换一条供应商。
PROVIDER_ID = "marvis-proxy"


class ZcodeAdapter(Adapter):
    agent_id = "zcode"
    display_name = "ZCode"
    files = (PROVIDER_CONFIG,)
    needs_restart = True
    shadows_env_keys = ()

    # ---- 小工具 ---------------------------------------------------------
    def _optional_files(self) -> list[str]:
        """只在用户本来就有的时候才碰的文件。"""
        present = [DESKTOP_CONFIG] if self.path(DESKTOP_CONFIG).exists() else []
        return present

    def all_paths(self) -> list[Path]:
        return [self.path(rel) for rel in (*self.files, *self._optional_files())]

    def _rule(self, model: str, effort: str | None) -> dict:
        facts = self.facts
        target = facts.require(model) if facts is not None else None
        model_id = target.id if target else model
        base = facts.openai_url if facts is not None else f"{self.gateway_url.rstrip('/')}/v1"
        name = facts.name if facts is not None else "Marvis Proxy"
        # 模型清单跟着网关走：用户改了 models.json，ZCode 下拉下次 apply 就同步。
        if facts is not None:
            ids = [m.id for m in facts.models]
        else:
            ids = [model_id]
        if model_id not in ids:
            ids = [model_id] + ids
        rule = {
            "providerId": PROVIDER_ID,
            "providerName": name,
            "enabled": True,
            "config": {
                "group": "standard-personal",
                "access": {"type": "api-key", "apiKey": self.gateway_key},
                "api": {
                    "type": "openai-chat-completions",
                    # ZCode 自己拼 /chat/completions，baseUrl 必须停在 /v1 且无尾斜杠，
                    # 否则请求发成 //v1/chat/completions 直接 404。
                    "baseUrl": base,
                },
                "personalModelIds": ids,
                "modelOrder": ids,
            },
        }
        return rule, model_id

    # ---- 钩子 -----------------------------------------------------------
    def plan(self, model: str, effort: str | None) -> list[Entry]:
        entries: list[Entry] = []
        for rel in (PROVIDER_CONFIG, *self._optional_files()):
            path = self.path(rel)
            text, _, _ = self.load_text(path)
            doc = _load(text)
            rules = (((doc.get("config") or {}).get("providerConfigRules") or {})
                     .get("providerRules") or [])
            present = any(r.get("providerId") == PROVIDER_ID for r in rules)
            original = next((r for r in rules if r.get("providerId") == PROVIDER_ID), None)
            order = (doc.get("config") or {}).get("providerOrder") or []
            selection = (doc.get("config") or {}).get("defaultModelSelection")
            entries.append(
                Entry(file=str(path), path=["__topkeys"], present=True,
                      value=json.dumps(list(doc), ensure_ascii=False), kind="zcode-topkeys")
            )
            entries.append(
                Entry(file=str(path), path=["__style"], present=True,
                      value=json.dumps(_style_of(text), ensure_ascii=False), kind="zcode-style")
            )
            entries.append(
                Entry(file=str(path), path=["providerRules", PROVIDER_ID], present=present,
                      value=json.dumps(original, ensure_ascii=False) if original else None,
                      kind="zcode-provider")
            )
            entries.append(
                Entry(file=str(path), path=["providerOrder", PROVIDER_ID],
                      present=PROVIDER_ID in order,
                      value=json.dumps(order, ensure_ascii=False),
                      kind="zcode-order")
            )
            entries.append(
                Entry(file=str(path), path=["defaultModelSelection"],
                      present=selection is not None,
                      value=json.dumps(selection, ensure_ascii=False) if selection else None,
                      kind="zcode-selection")
            )
        return entries

    def render(self, path: Path, text: str, newline: str, entries: list[Entry], model: str,
               effort: str | None) -> str:
        doc = _load(text)
        config = doc.setdefault("config", {})
        before = json.loads(text) if text.strip() else {}
        if "schemaVersion" not in before:
            # 新建的文件打上版本号；已有的保留它自己声明的。
            doc.setdefault("schemaVersion", 1)
        rules = config.setdefault("providerConfigRules", {}).setdefault("providerRules", [])

        rule, model_id = self._rule(model, effort)
        for index, existing in enumerate(rules):
            if existing.get("providerId") == PROVIDER_ID:
                # 用户自己写在这条规则里的东西（自定义图标、隐藏标记）原样保留，只覆盖
                # 我们负责的连接信息。
                merged = dict(existing)
                merged.update(rule)
                merged["config"] = {**existing.get("config", {}), **rule["config"]}
                rules[index] = merged
                break
        else:
            rules.append(rule)

        order = config.setdefault("providerOrder", [])
        if PROVIDER_ID not in order:
            order.append(PROVIDER_ID)

        model_config_rules = config.setdefault("modelConfigRules", {})
        # 两个数组即使为空也必须存在，ZCode 会校验它们在不在。
        model_config_rules.setdefault("providerModelRules", [])
        model_config_rules.setdefault("manualProviderModelRules", [])

        selection: dict = {"providerId": PROVIDER_ID, "modelId": model_id}
        if effort:
            selection["options"] = {"reasoningLevel": effort}
        config["defaultModelSelection"] = selection

        return _dump(doc, text, newline)

    def unrender(self, path: Path, text: str, newline: str, entries: list[Entry]) -> str:
        style_entry = next((e for e in entries if e.kind == "zcode-style"), None)
        style = json.loads(style_entry.value) if style_entry is not None and style_entry.value else None
        if style is not None and style.get("empty"):
            # 快照时文件是空的：现在里面的东西都是我们写的，清空而不是留个 {}。
            return ""
        doc = _load(text)
        if not (text or "").strip():
            return ""
        config = doc.get("config") or {}

        rules = (config.get("providerConfigRules") or {}).get("providerRules") or []
        provider_entry = next((e for e in entries if e.kind == "zcode-provider"), None)
        if provider_entry is not None:
            rules = [r for r in rules if r.get("providerId") != PROVIDER_ID]
            if provider_entry.present and provider_entry.value:
                # 用户本来就有同 id 的 provider，把他们的放回去。
                rules.append(json.loads(provider_entry.value))
            config.setdefault("providerConfigRules", {})["providerRules"] = rules

        order_entry = next((e for e in entries if e.kind == "zcode-order"), None)
        if order_entry is not None:
            if order_entry.present and order_entry.value is not None:
                config["providerOrder"] = json.loads(order_entry.value)
            else:
                config["providerOrder"] = [p for p in (config.get("providerOrder") or [])
                                           if p != PROVIDER_ID]
                if not config["providerOrder"]:
                    config.pop("providerOrder", None)

        selection_entry = next((e for e in entries if e.kind == "zcode-selection"), None)
        if selection_entry is not None:
            if selection_entry.present and selection_entry.value:
                config["defaultModelSelection"] = json.loads(selection_entry.value)
            elif not selection_entry.present:
                config.pop("defaultModelSelection", None)

        model_config_rules = config.get("modelConfigRules") or {}
        if not model_config_rules.get("providerModelRules"):
            model_config_rules.pop("providerModelRules", None)
        if not model_config_rules.get("manualProviderModelRules"):
            model_config_rules.pop("manualProviderModelRules", None)
        if not model_config_rules:
            config.pop("modelConfigRules", None)

        # `config` 下面可能整个是我们搭的脚手架：providerConfigRules 外壳、空的
        # `config`、甚至原本一个键都没有时的 schemaVersion。自底向上清掉，回滚才逐字节
        # 一致。
        config_rules = config.get("providerConfigRules")
        if isinstance(config_rules, dict) and not config_rules.get("providerRules"):
            config.pop("providerConfigRules", None)
        if not config:
            doc.pop("config", None)
        # 与动手前记下的键序比：这里的 text 是我们写完之后的文件，仍带着我们加的东西。
        # 按原序重排。
        recorded = next((e for e in entries if e.kind == "zcode-topkeys"), None)
        before = json.loads(recorded.value) if recorded is not None and recorded.value else None
        if before is not None:
            for key in [k for k in doc if k not in before]:
                doc.pop(key, None)
            ordered = {k: doc[k] for k in before if k in doc}
            ordered.update({k: v for k, v in doc.items() if k not in before})
            return _dump(ordered, text, newline, style)
        return _dump(doc, text, newline, style)


def _load(text: str) -> dict:
    text = text.strip()
    if not text:
        return {"config": {}}
    doc = json.loads(text)
    if not isinstance(doc, dict):
        raise ValueError("provider_config.json: expected a JSON object at the top level")
    return doc


_ARRAY_RE = re.compile(r"\[\s*(?:[^\[\]{},]+(?:,\s*[^\[\]{},]+)*)?\s*\]", re.S)
_OBJECT_RE = re.compile(r"\{[^{}]*\}", re.S)


def _dump(doc: dict, original: str, newline: str, style: dict | None = None) -> str:
    """按文件自己的排版序列化，改一个键就只差一行。

    ``style`` 补的是光看 ``original`` 推不出来的东西（回滚时拿到的文件已被我们改过）：
    缩进、``:`` 后的分隔符、哪些子对象原本写在一行。
    """
    style = style or {}
    original = original if original.strip() else ""
    indent = style.get("indent", _indent(original))
    if indent is None:
        out = json.dumps(doc, ensure_ascii=False, separators=(", ", ": ") if style.get("separator") == ", " else (",", ":"))
    else:
        out = json.dumps(doc, indent=indent, ensure_ascii=False, separators=(",", ": "))
    if indent is not None and original:
        inline = style.get("inline") if "inline" in style else _inline_paths(original)
        for path in sorted(inline, key=len, reverse=True):
            out = _collapse_value(out, path)
    if newline == "\r\n":
        out = out.replace("\n", "\r\n")
    out = to_newline(out, newline)
    if style.get("trailing_newline", original.endswith("\n") or not original):
        return out.rstrip("\r\n") + newline if out.strip() else out
    return out.rstrip("\r\n")


def _split_sep(separator: str, indent) -> tuple[str, str]:
    """``json.dumps`` 的两个分隔符：(项之间, 键值之间)。

    只有项分隔符是真选择（有的文件写 ``[1, 2]``，有的写 ``[1,2]``）；键值分隔符在缩进
    输出里恒为 ``": "``、紧凑形式里恒为 ``":"``。注意 ``json.dumps`` 带缩进时会把项分隔
    符接在逗号后面，所以传 ``", "`` 会在每个非末键后留下尾随空格 —— 这里两者都钉死，
    不从文件推。
    """
    if indent is None:
        return (", ", ": ") if separator == ", " else (",", ":")
    return (",", ": ")


def _style_of(text: str) -> dict:
    """``json.dumps`` 会抹平的全部排版信息。"""
    trailing = text.endswith("\n")
    if not text.strip():
        return {"indent": None, "separator": ",", "inline": [], "trailing_newline": trailing,
                "empty": True}
    body = text.strip()
    if "\n" not in body:
        return {"indent": None, "separator": ", " if ", " in body else ",",
                "inline": [], "trailing_newline": trailing}
    seen = re.search(r'"[^"]*":(\s+)', body)
    separator = ", " if (seen and seen.group(1) == " ") else ","
    return {"indent": _indent(text), "separator": separator,
            "inline": sorted(_inline_paths(text), key=len, reverse=True),
            "trailing_newline": trailing}


def _inline_paths(text: str) -> list:
    """用户原本写在一行上的 ``{...}``/``[...]`` 值所在路径。

    走解析树而不是正则扫文本：两处行内值可能字面完全相同（``{"a": 1}`` 出现不止一次），
    文本匹配分不清哪个属于哪条路径。
    """
    paths = []
    try:
        data = json.loads(text)
    except ValueError:
        return paths

    def walk(node, path, raw):
        if isinstance(node, dict):
            children = list(node.items())
        elif isinstance(node, list):
            children = list(enumerate(node))
        else:
            return
        if isinstance(node, dict) and isinstance(raw, _Raw) and isinstance(raw.data, dict):
            raws = [(k, raw.data.get(k)) for k, _ in children]
        elif isinstance(node, list) and isinstance(raw, _Raw) and isinstance(raw.data, list):
            raws = [(i, raw.data[i] if i < len(raw.data) else None) for i, _ in children]
        elif isinstance(node, dict):
            raws = [(k, None) for k, _ in children]
        elif isinstance(node, list):
            raws = [(i, None) for i, _ in children]

        for (key, value), (_, raw_child) in zip(children, raws):
            walk(value, path + (key,), raw_child)
        src = raw.src if isinstance(raw, _Raw) else None
        if node and src is not None and "\n" not in src:
            paths.append(path)

    walk(data, (), _inline_nodes(text))
    return paths


def _inline_nodes(text: str) -> object:
    """重新解析 ``text``，为每个容器保留它自己的源文本。"""
    body = text.strip()
    depth_stack: list[str] = []

    def parse(pos: int):
        while pos < len(body) and body[pos] in " \t\r\n":
            pos += 1
        start = pos
        if body[pos] == "{":
            out: dict = {}
            pos += 1
            while True:
                while pos < len(body) and body[pos] in " \t\r\n,":
                    pos += 1
                if body[pos] == "}":
                    pos += 1
                    break
                key, pos = parse(pos)
                while pos < len(body) and body[pos] in " \t\r\n:":
                    pos += 1
                value, pos = parse(pos)
                out[key] = value
            return _Raw(body[start:pos], out), pos
        if body[pos] == "[":
            out_list: list = []
            pos += 1
            while True:
                while pos < len(body) and body[pos] in " \t\r\n,":
                    pos += 1
                if body[pos] == "]":
                    pos += 1
                    break
                value, pos = parse(pos)
                out_list.append(value)
            return _Raw(body[start:pos], out_list), pos
        if body[pos] == '"':
            pos += 1
            buf = []
            while body[pos] != '"':
                if body[pos] == "\\":
                    buf.append(body[pos:pos + 2])
                    pos += 2
                    continue
                buf.append(body[pos])
                pos += 1
            pos += 1
            return json.loads('"' + "".join(buf) + '"'), pos
        for lit, value in (("true", True), ("false", False), ("null", None)):
            if body.startswith(lit, pos):
                return value, pos + len(lit)
        end = pos
        while end < len(body) and body[end] not in ",]}\r\n \t":
            end += 1
        return json.loads(body[pos:end]), end

    return parse(0)[0]


class _Raw:
    """解析出的值 + 它来自的那段源文本。"""

    __slots__ = ("src", "data")

    def __init__(self, src: str, data):
        self.src = src
        self.data = data


def _collapse_value(text: str, path: tuple) -> str:
    """在已排好版的内容里，把某个值折回一行。

    只在快照里记过的行内路径上跑；括号层级对不上就原样返回，不硬折。
    """
    if not path:
        return text
    lines = text.split("\n")
    key = path[-1]
    target = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if isinstance(key, int):
            parent = f'"{path[-2]}": [' if len(path) >= 2 and isinstance(path[-2], str) else None
            if parent and lines[i - 1].strip().startswith(parent):
                target = i
                break
        elif stripped.startswith(f'"{key}": ') and stripped.endswith(("[", "{")):
            target = i
            break
    if target is None:
        return text
    depth = 0
    end = None
    for j in range(target, len(lines)):
        depth += lines[j].count("[") + lines[j].count("{")
        depth -= lines[j].count("]") + lines[j].count("}")
        if depth <= 0:
            end = j
            break
    if end is None or end <= target:
        return text
    head = lines[target]
    lead = head[:len(head) - len(head.lstrip())]
    body = "".join(part.strip() for part in lines[target:end + 1])
    body = body.strip().replace(",", ", ").replace(",  ", ", ")
    while ", ]" in body or ", }" in body or ",]" in body or ",}" in body:
        body = body.replace(", ]", "]").replace(", }", "}").replace(",]", "]").replace(",}", "}")
    folded = (lead + body).rstrip()
    if not folded.endswith((",",)):
        nxt = lines[end + 1].strip() if end + 1 < len(lines) else ""
        if nxt.startswith('"') and ":" in nxt:
            folded += ","
    lines[target:end + 1] = [folded]
    return "\n".join(lines)


def _indent(text: str):
    """沿用文件自己的排版：没有子行、紧凑形式、tab 或 N 个空格。"""
    if not text.strip():
        return 2
    if "\n" not in text.strip():
        # 单行文件必须还是单行文件。
        return None
    for line in text.splitlines()[1:]:
        stripped = line.strip()
        if stripped:
            prefix = line[:len(line) - len(line.lstrip())]
            return "\t" if prefix.startswith("\t") else len(prefix)
    return 2
