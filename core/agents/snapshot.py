# -*- coding: utf-8 -*-
"""原值快照：存在 marvis.db 里，一份 agent 一行。

    表 wire_snapshots(agent PRIMARY KEY, json, updated_at)

为什么进库、不像 magpie-min 那样一个 agent 一个 JSON 文件：
  · 反代已经有 Store / sqlite / 备份链，backups/marvis-*.tar.gz 会顺带把快照一起备走，
    用户「回滚到上周的配置」时凭据不会单独丢一份；
  · 一个库文件比 snapshots/ 一层目录好审计——`SELECT * FROM wire_snapshots` 就能看见
    现在哪个 agent 的配置被我们改过。

文档形状（json 列）::

    {
      "agent": "claude-code",
      "created_at": "2026-11-20T07:00:00+0800",
      "gateway": {"id": "marvis-proxy", "base": "http://127.0.0.1:8088",
                  "anthropic": "http://127.0.0.1:8088", "openai": "http://127.0.0.1:8088/v1"},
      "files": {
        "C:/Users/x/.claude/settings.json": {
          "existed": true, "mode": 384,
          "hash_original": "…",    # 第一次见到它时的内容
          "hash_written": "…",     # 我们最后一次写完之后的内容
          "backup": "C:/…/data/wire-backups/claude-code/0-original/settings.json",
          "meta": {"trailing_newline": true}
        }
      },
      "entries": [{"file": "...", "path": ["env", "ANTHROPIC_BASE_URL"],
                   "present": false, "value": null, "kind": "json"}],
      "created_files": ["..."],
      "applied": {"model": "...", "effort": "high", "at": "..."}
    }

回滚只读这份文档 + 它旁边的逐字节备份。它**不猜**「哪几行看起来像我们写的」：
每个 entry 都记着这个键原来在不在、原来是什么值，每个文件都记着是不是我们新建的。
"""
from __future__ import annotations

import json
import time
from typing import Any

from . import atomic

_DDL = """
CREATE TABLE IF NOT EXISTS wire_snapshots (
  agent      TEXT PRIMARY KEY,
  json       TEXT DEFAULT '',
  updated_at REAL DEFAULT 0
);
"""


class SnapshotStore(object):
    """和 Store 同一个用法：拿 ctx["store"].conn() 落库。"""

    def __init__(self, store, backup_root):
        self.store = store
        self.backup_root = atomic._paths.abs(backup_root)
        store.conn().executescript(_DDL)

    # ---- 文档 ---------------------------------------------------------
    def load(self, agent):
        row = self.store.q1("SELECT json FROM wire_snapshots WHERE agent=?", (agent,))
        if row is None or not row["json"]:
            return None
        try:
            return json.loads(row["json"])
        except ValueError:
            return None

    def save(self, data):
        blob = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False)
        self.store.x(
            "INSERT INTO wire_snapshots(agent,json,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(agent) DO UPDATE SET json=excluded.json, updated_at=excluded.updated_at",
            (data["agent"], blob, time.time()),
        )

    def exists(self, agent):
        return self.load(agent) is not None

    def delete(self, agent):
        self.store.x("DELETE FROM wire_snapshots WHERE agent=?", (agent,))

    def agents(self):
        return [r["agent"] for r in self.store.q("SELECT agent FROM wire_snapshots ORDER BY agent")]

    # ---- 逐字节备份 ---------------------------------------------------
    def backup_path(self, agent, index, file_path):
        name = str(file_path).replace("\\", "/").rstrip("/").split("/")[-1]
        return atomic._paths.abs(
            "%s/%s/%d-original/%s" % (self.backup_root, agent, index, name)
        )

    def write_backup(self, agent, index, file_path, data, mode=None):
        target = self.backup_path(agent, index, file_path)
        atomic.atomic_write_bytes(target, data, mode)
        return target

    def read_backup(self, backup):
        return atomic.read_bytes(backup)

    def purge_backups(self, agent):
        folder = atomic._paths.abs("%s/%s" % (self.backup_root, agent))
        _rmtree(folder)


def _rmtree(folder):
    import shutil

    try:
        shutil.rmtree(folder)
    except OSError:
        pass


def make_entry(file, path, present, value=None, kind="json", after=None):
    out = {"file": file, "path": list(path), "present": present,
           "value": None if not present else value, "kind": kind}
    if after is not None:
        out["after"] = after
    return out


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
