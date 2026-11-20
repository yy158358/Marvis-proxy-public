# -*- coding: utf-8 -*-
"""Agent 配置接线：把「谁去连我们这个网关」变成一次可撤销的事务。

这一层解决的是**作家务**的问题，不是**协议**的问题。网关自己知道模型清单和真实
数值（``models.json``），所以这里不需要猜任何一个数字；需要保证的只有四件事：

* 原值快照 —— 第一次写之前记下每个键原来在不在、是什么；回滚照快照还原，绝不靠
  「哪几行看起来像我们写的」去猜。
* 原子写 —— 临时文件 → fsync → rename。Agent 在读取时拿到半截文件就是全组不可用。
* 保格式写 —— TOML 按字节区间拼接、JSON 保留缩进/分隔符/键序、YAML 逐行编辑，用户的
  注释和排版不动。
* 漂移检测 —— 每次动手前重读文件，和我们上次写的内容比；不一致就报错要 ``--force``，
  不静默覆盖。

另外一条硬规矩：**key 只从调用方给的值或凭据库来，绝不从 shell 环境读**。
环境里的同名变量只会被检查出来并警告，因为那是「会把配置压掉」的东西，不是配置源。
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
    """按「你大概最常接的」顺序列出适配器 id。"""
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
    """哪些 agent 在这台机器上装了（看配置文件在不在）。"""
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
