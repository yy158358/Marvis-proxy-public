# -*- coding: utf-8 -*-
"""Codex CLI：``~/.codex/config.toml`` + 模型目录文件。

两点与 Claude Code 不同：

1. Codex 启动时按它认识的 provider 校验 ``model``，所以模型清单要单独落一个 JSON
   （``model_catalog_json``），也因此 **改完必须重启 Codex**。
2. Codex 说 Responses API，marvis-proxy 说 chat-completions。这里不做协议猜测：
   provider 块写 ``wire_api = "chat"``，直接走现成的 ``/v1/chat/completions``。
   写 ``"responses"`` 会连到不存在的端点，报错还像 model 名字错了。

按「是否登录了 ChatGPT 套餐」分两种形状：登录了只改 ``openai_base_url`` 和 ``model``，
保留自带 provider；没登录就注册一个 provider 并设为当前。key 放在 provider 表里，不落
环境变量。
"""
from __future__ import annotations

import json

from .atomic import to_newline
from .base import Adapter, Entry
from .edit import (
    toml_delete_key,
    toml_find_key,
    toml_has_table,
    toml_insert_table,
    toml_quote,
    toml_remove_table,
    toml_set_key,
    toml_set_table,
    toml_table_neighbours,
)

CONFIG = ".codex/config.toml"
CATALOG = ".codex/marvis-models.json"

MODEL = "model"
EFFORT = "model_reasoning_effort"
PROVIDER = "model_provider"
OPENAI_BASE_URL = "openai_base_url"
CATALOG_KEY = "model_catalog_json"
PROVIDER_ID = "marvis-proxy"
PROVIDER_TABLE = "model_providers.marvis-proxy"
TOP_KEYS = (MODEL, EFFORT, PROVIDER, OPENAI_BASE_URL, CATALOG_KEY)

#: provider 表示层。取 chat 而非 responses，理由见模块开头。
WIRE_API = "chat"
#: Codex 只认这几档，给多了会被静默丢弃。
EFFORTS = ("minimal", "low", "medium", "high")


class CodexAdapter(Adapter):

    agent_id = "codex"
    display_name = "Codex"
    files = (CONFIG, CATALOG)
    needs_restart = True
    shadows_env_keys = ("OPENAI_BASE_URL", "OPENAI_API_KEY")

    # ---- 环境 -----------------------------------------------------------
    def _signed_in(self):
        """Codex 把 ChatGPT 登录态存在配置旁边，存在即视为已登录。"""
        return self.path(".codex/auth.json").exists()

    def catalog_path(self):
        return self.path(CATALOG)

    def catalog(self, model):
        """整份模型清单。Codex 从这里读上下文长度和输出上限，不读我们的 models.json。"""
        facts = self.facts
        rows = []
        for item in (facts.models if facts is not None else []):
            rows.append({
                "slug": item.id,
                "display_name": item.display,
                "context_window": item.context_window or 200000,
                "max_output_tokens": item.max_output_tokens or 32000,
                "supported_reasoning_efforts": list(EFFORTS),
            })
        if not rows:
            rows.append({
                "slug": model, "display_name": model, "context_window": 200000,
                "max_output_tokens": 32000, "supported_reasoning_efforts": list(EFFORTS),
            })
        return {"models": rows}

    # ---- 钩子 -----------------------------------------------------------
    def plan(self, model, effort):
        path = self.path(CONFIG)
        text, _, _ = self.load_text(path)
        entries = []
        for key in TOP_KEYS:
            found = toml_find_key(text, key, None)
            entries.append(Entry(file=str(path), path=[key], present=found is not None,
                                 value=found[2] if found else None, kind="toml"))
        # provider 表全有全无：记下在不在、原文、以及它前面是哪张表 —— 回滚要放回原位，
        # 不是追加到文件末尾。
        has_table = toml_has_table(text, PROVIDER_TABLE)
        before, _after = toml_table_neighbours(text, PROVIDER_TABLE)
        entries.append(Entry(file=str(path), path=PROVIDER_TABLE.split("."),
                             present=has_table,
                             value=_table_body(text, PROVIDER_TABLE) if has_table else None,
                             kind="toml-table", after=before))
        catalog = self.catalog_path()
        entries.append(Entry(file=str(catalog), path=["*"], present=catalog.exists(),
                             value=None, kind="own-file"))
        return entries

    def render(self, path, text, newline, entries, model, effort):
        facts = self.facts
        target = facts.require(model) if facts is not None else None
        model_id = target.id if target else model
        base = facts.openai_url if facts else self.gateway_url + "/v1"

        if path.name == CATALOG.split("/")[-1]:
            return json.dumps(self.catalog(model_id), indent=2, ensure_ascii=False) + "\n"

        text = toml_set_key(text, MODEL, None, toml_quote(model_id))
        if self._signed_in():
            # 保留自带的 ChatGPT provider，只把端点和模型挪走。
            text = toml_set_key(text, OPENAI_BASE_URL, None, toml_quote(base))
            text = toml_delete_key(text, PROVIDER, None)
            text = toml_delete_key(text, CATALOG_KEY, None)
            text = toml_remove_table(text, PROVIDER_TABLE)
        else:
            text = toml_set_key(text, PROVIDER, None, toml_quote(PROVIDER_ID))
            text = toml_set_key(text, CATALOG_KEY, None, toml_quote(str(self.catalog_path())))
            text = toml_set_table(text, PROVIDER_TABLE, {
                "name": toml_quote(facts.name if facts else PROVIDER_ID),
                "base_url": toml_quote(base),
                "wire_api": toml_quote(WIRE_API),
                "api_key": toml_quote(self.gateway_key),
            })
        if effort:
            text = toml_set_key(text, EFFORT, None, toml_quote(effort))
        return to_newline(text, newline)

    def unrender(self, path, text, newline, entries):
        if path.name == CATALOG.split("/")[-1]:
            return text
        for entry in entries:
            key = entry.path if isinstance(entry.path, list) else [entry.path]
            if len(key) == 2 and key[0] == "model_providers":
                if entry.present:
                    text = _restore_table(text, PROVIDER_TABLE, entry.value or "", entry.after)
                else:
                    text = toml_remove_table(text, PROVIDER_TABLE)
                continue
            if key == ["*"]:
                continue
            if entry.present:
                text = toml_set_key(text, key[0], None, str(entry.value))
            else:
                text = toml_delete_key(text, key[0], None)
        return to_newline(text, newline)


def _table_body(text, table):
    """``[table]`` 里的行，原样，不含表头。"""
    out = []
    inside = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            inside = stripped.strip("[]").strip() == table
            continue
        if inside:
            out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def _restore_table(text, table, body, after):
    """把表放回原位：``after`` 是快照里它前面那张表，保持表序而不挪到文件末尾。"""
    if not body.strip():
        return toml_remove_table(text, table)
    text = toml_remove_table(text, table)
    entries = _parse_body(body)
    if after:
        _before, nxt = toml_table_neighbours(text, after)
        if nxt:
            return toml_insert_table(text, table, entries, nxt)
    return toml_set_table(text, table, entries)


def _parse_body(body):
    out = {}
    for line in body.splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out
