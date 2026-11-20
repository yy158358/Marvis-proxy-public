# -*- coding: utf-8 -*-
"""看一眼最近的 usage 记录（谁在用反代）。"""
import datetime
import os
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
db = os.path.join(ROOT, "data", "marvis.db")
c = sqlite3.connect(db)
c.row_factory = sqlite3.Row
rows = c.execute("select at, model, status, total_tokens, ms from usage order by at desc limit 15").fetchall()
for r in rows:
    print("%s  %-28s %s  tok=%-6s %sms" % (
        datetime.datetime.fromtimestamp(r["at"]).strftime("%m-%d %H:%M:%S"),
        r["model"], r["status"], r["total_tokens"], r["ms"]))
