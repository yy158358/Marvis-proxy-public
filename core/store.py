# -*- coding: utf-8 -*-
"""sqlite 存储层：单文件库 + 每线程一个连接。

为什么每线程一个连接：serve.py 是 ThreadingHTTPServer，请求线程互不共享连接；
sqlite3 默认 check_same_thread=True，跨线程用同一连接会直接抛异常。
"""
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  openid        TEXT UNIQUE NOT NULL,
  access_token  TEXT NOT NULL,
  refresh_token TEXT DEFAULT '',
  guid          TEXT DEFAULT '',
  login_type    TEXT DEFAULT 'QC',
  appid         TEXT DEFAULT '',
  scope         TEXT DEFAULT '',
  status        TEXT DEFAULT 'active',      -- active / disabled
  weight        INTEGER DEFAULT 100,        -- 100 满血，401 归零+冷却，429/5xx 扣，成功回涨
  cooldown_until REAL DEFAULT 0,
  fail_count    INTEGER DEFAULT 0,
  ok_count      INTEGER DEFAULT 0,
  last_used_at  REAL DEFAULT 0,
  created_at    REAL DEFAULT 0,
  refreshed_at  REAL DEFAULT 0,
  expires_in    INTEGER DEFAULT 0,          -- 上游给的秒数，仅展示
  note          TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS tasks (
  id           TEXT PRIMARY KEY,
  client_key   TEXT DEFAULT '',
  model        TEXT DEFAULT '',
  size         TEXT DEFAULT '',
  prompt       TEXT DEFAULT '',
  state        TEXT DEFAULT 'queued',       -- queued/submitted/working/completed/failed
  error        TEXT DEFAULT '',
  file         TEXT DEFAULT '',
  account_id   INTEGER DEFAULT 0,
  conv_id      TEXT DEFAULT '',
  upstream_id  TEXT DEFAULT '',
  created_at   REAL DEFAULT 0,
  updated_at   REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state, created_at);

CREATE TABLE IF NOT EXISTS usage (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  at                REAL DEFAULT 0,
  iso               TEXT DEFAULT '',
  model             TEXT DEFAULT '',
  slot              TEXT DEFAULT '',
  account_id        INTEGER DEFAULT 0,
  prompt_tokens     INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0,
  total_tokens      INTEGER DEFAULT 0,
  ms                INTEGER DEFAULT 0,
  stream            INTEGER DEFAULT 0,
  status            INTEGER DEFAULT 200,
  kind              TEXT DEFAULT 'chat',
  reasoning_tokens  INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_at ON usage(at);

CREATE TABLE IF NOT EXISTS login_pending (
  state      TEXT PRIMARY KEY,
  guid       TEXT DEFAULT '',
  login_type TEXT DEFAULT 'QC',
  at         REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS client_keys (
  key        TEXT PRIMARY KEY,
  note       TEXT DEFAULT '',
  enabled    INTEGER DEFAULT 1,
  created_at REAL DEFAULT 0,
  kind       TEXT DEFAULT 'user',
  plain      TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS sanitize_plan (
  fp TEXT PRIMARY KEY,
  level INTEGER DEFAULT 0,
  rule_ver REAL DEFAULT 0,
  updated_at REAL
);

CREATE TABLE IF NOT EXISTS sessions (
  key        TEXT DEFAULT '',
  session_id TEXT DEFAULT '',
  account_id INTEGER DEFAULT 0,
  conv_id    TEXT DEFAULT '',
  at         REAL DEFAULT 0,
  PRIMARY KEY (key, session_id)
);
"""


def now():
    return time.time()


def iso(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now() if ts is None else ts))


class Store(object):
    def _migrate(self):
        """老库补列：sqlite 没有 ADD COLUMN IF NOT EXISTS，只能先查再 ALTER。"""
        want = {
            "usage": [("reasoning_tokens", "INTEGER DEFAULT 0")],
            "tasks": [("retries", "INTEGER DEFAULT 0"), ("error_kind", "TEXT DEFAULT ''")],
            "client_keys": [("prefix", "TEXT DEFAULT ''"), ("last_used_at", "REAL DEFAULT 0"),
                            ("uses", "INTEGER DEFAULT 0"),
                            # 'user' 普通人建的；'wire' 一键接入自动发的那把（见 api/keys.py）。
                            # 老库存量行的 kind 是空串，一律按 user 处理。
                            ("kind", "TEXT DEFAULT 'user'"),
                            # 只有 kind='wire' 才存明文：这把钥匙每次接线都要写进配置
                            # 文件，而用户看不到它，所以没法「只显示一次」。普通 key
                            # 这一列永远是空串，仍然只有 sha256。
                            ("plain", "TEXT DEFAULT ''")],
            "sanitize_plan": [("rule_ver", "REAL DEFAULT 0")],
        }
        for table, cols in want.items():
            try:
                have = [r[1] for r in self.conn().execute("PRAGMA table_info(%s)" % table)]
                for name, decl in cols:
                    if name not in have:
                        self.conn().execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))
                self.conn().commit()
            except Exception:
                pass

    def __init__(self, path):
        self.path = path
        d = os.path.dirname(os.path.abspath(path))
        if d and not os.path.isdir(d):
            os.makedirs(d)
        self._local = threading.local()
        # executescript 会自己提交事务，不能包在显式 BEGIN 里
        self.conn().executescript(SCHEMA)
        self._migrate()

    # ---- 连接 ----
    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=20, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=8000")
            self._local.conn = c
        return c

    # ---- 查询 ----
    def q(self, sql, args=()):
        return self.conn().execute(sql, args).fetchall()

    def q1(self, sql, args=()):
        return self.conn().execute(sql, args).fetchone()

    def x(self, sql, args=()):
        return self.conn().execute(sql, args).rowcount
