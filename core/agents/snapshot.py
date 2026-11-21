# -*- coding: utf-8 -*-
"""原值快照：一个 agent 一行，存在 marvis.db 的 ``wire_snapshots`` 表里。

为什么进库而不是像 magpie-min 那样一个 agent 一个 JSON 文件：
  · 反代已有 sqlite 与备份链，``backups/marvis-*.tar.gz`` 顺带把快照备走；
  · ``SELECT * FROM wire_snapshots`` 就能看见现在改过谁的配置，好审计。

文档形状（json 列）::

    {
      "agent": "claude-code",
      "created_at": "2026-11-20T07:00:00+0800",
      "files": {
        "C:/Users/x/.claude/settings.json": {
          "existed": true, "mode": 384,
          "hash_original": "…",   # 第一次见到它时的内容
          "hash_written": "…",    # 我们最后一次写完之后的内容
          "backup": "…/wire-backup/claude-code/0-original/settings.json",
          "meta": {"trailing_newline": true}
        }
      },
      "entries": [{"file": "...", "path": ["env", "ANTHROPIC_BASE_URL"],
                   "present": false, "value": null, "kind": "json"}],
      "created_files": ["..."],
      "applied": {"model": "...", "effort": "high", "at": "..."}
    }

回滚只读这份文档加旁边的逐字节备份，不猜「哪几行像我们写的」：每个 entry 都记着
键原来在不在、值是什么，每个文件都记着是不是我们新建的。
"""
from __future__ import annotations

import json
import time

from . import atomic

_DDL = """
CREATE TABLE IF NOT EXISTS wire_snapshots (
  agent      TEXT PRIMARY KEY,
  json       TEXT DEFAULT '',
  updated_at REAL DEFAULT 0
);
"""


class SnapshotStore(object):
    """和 Store 一个用法，走 ``ctx["store"].conn()`` 落库。"""

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
        blob = json.dumps(data, indent=2, ensure_ascii=False)
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
        _rmtree(atomic._paths.abs("%s/%s" % (self.backup_root, agent)))


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
