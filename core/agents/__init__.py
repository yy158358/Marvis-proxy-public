# -*- coding: utf-8 -*-
"""Agent 配置接线：把「谁去连本网关」变成一次可撤销的事务。

这一层管的是作家务，不是协议。模型清单和真实数值都在 ``models.json`` 里，这里不
猜任何数字，只保证四件事：

* 原值快照 —— 首次写之前记下每个键原来在不在、是什么；回滚照快照还原，不靠「哪几
  行像我们写的」去推断。
* 原子写 —— 临时文件 → fsync → rename。Agent 读到半截文件就是全组不可用。
* 保格式写 —— TOML 按字节区间拼接、JSON 保留缩进分隔符与键序、YAML 逐行编辑，
  注释和排版不动。
* 漂移检测 —— 每次动手前重读文件，与上次写的内容比；不一致就报错要 ``force``。

另一条硬规矩：key 只来自调用方给的值或凭据库，**绝不从 shell 环境读**。环境里的
同名变量只做检测与告警，因为它会压掉配置，不是配置源。
"""
from __future__ import annotations

import os

from . import paths
from .base import Adapter, ApplyResult, DriftError, Entry, RollbackResult
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .dsh import DshAdapter
from .facts import ANTHROPIC, OPENAI, Facts, Model
from .snapshot import SnapshotStore, make_entry, now_iso
from .zcode import ZcodeAdapter

CLAUDE_CODE = "claude-code"
CODEX = "codex"
DSH = "dsh"
ZCODE = "zcode"

REGISTRY = {
    CLAUDE_CODE: ClaudeCodeAdapter,
    CODEX: CodexAdapter,
    DSH: DshAdapter,
    ZCODE: ZcodeAdapter,
}


def names():
    """按最常接的顺序列出适配器 id。"""
    return [CLAUDE_CODE, CODEX, DSH, ZCODE]


def get_adapter(agent_id):
    cls = REGISTRY.get(agent_id)
    if cls is None:
        raise LookupError("未知 agent %r；可选：%s" % (agent_id, ", ".join(names())))
    return cls


def build(agent_id, home, gateway_url, gateway_key, store, facts=None):
    return get_adapter(agent_id)(paths.abs(home), gateway_url, gateway_key, store, facts=facts)


def build_all(home, gateway_url, gateway_key, store, facts=None):
    return {agent_id: build(agent_id, home, gateway_url, gateway_key, store, facts)
            for agent_id in names()}


def detect(home=None):
    """哪些 agent 装在这台机器上（看配置文件在不在）。"""
    home = paths.abs(home or paths.home())
    out = {}
    for agent_id in names():
        cls = get_adapter(agent_id)
        out[agent_id] = any(os.path.exists(os.path.join(home, rel)) for rel in cls.files)
    return out


__all__ = [
    "Adapter",
    "ApplyResult",
    "DriftError",
    "Entry",
    "RollbackResult",
    "SnapshotStore",
    "Facts",
    "Model",
    "ANTHROPIC",
    "OPENAI",
    "REGISTRY",
    "CLAUDE_CODE",
    "CODEX",
    "DSH",
    "ZCODE",
    "names",
    "get_adapter",
    "build",
    "build_all",
    "detect",
    "make_entry",
    "now_iso",
]
