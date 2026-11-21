# -*- coding: utf-8 -*-
"""看一眼最近的生图任务（tasks 表）。

    py -3 tools\\task_tail.py                     最近 15 条
    py -3 tools\\task_tail.py -n 50               最近 50 条
    py -3 tools\\task_tail.py --state failed      只看失败的
    py -3 tools\\task_tail.py --id img_fb0ac3c2d75040f2bc2f   按 id 看一条（含 error 全文）
    py -3 tools\\task_tail.py --prompt            连 prompt 一起打（默认只打前 40 字）

tasks 表由 api/openai.py 的异步生图流程写入，行数 = 累计提交过的生图任务数。
"""
import argparse
import datetime
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "marvis.db")

COLS = ("id", "state", "model", "size", "account_id", "created_at", "updated_at", "file")


def fmt_ts(v):
    if not v:
        return "-"
    if isinstance(v, (int, float)):
        try:
            return datetime.datetime.fromtimestamp(v).strftime("%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return str(v)
    return str(v)[:19]


def main():
    ap = argparse.ArgumentParser(description="看一眼最近的生图任务（tasks 表）")
    ap.add_argument("-n", "--limit", type=int, default=15, help="显示条数，默认 15")
    ap.add_argument("--state", default="", help="只显示某个状态：completed / failed / running")
    ap.add_argument("--id", default="", help="只看这一条（tasks.id，形如 img_fb0ac3c2...）")
    ap.add_argument("--prompt", action="store_true", help="打印完整 prompt（默认截断到 40 字）")
    a = ap.parse_args()

    if not os.path.exists(DB):
        print("没有数据库：%s" % DB)
        print("先 run 一次（默认目录 data/，可用 MARVIS_DATA_DIR 改）再回来看。")
        return 2

    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        has_tasks = c.execute(
            "select count(*) from sqlite_master where type='table' and name='tasks'"
        ).fetchone()[0]
        if not has_tasks:
            print("库里没有 tasks 表（还没提交过生图任务）。")
            return 0

        where, args = [], []
        if a.id:
            where.append("id = ?")
            args.append(a.id)
        if a.state:
            where.append("state = ?")
            args.append(a.state)
        sql = "select * from tasks"
        if where:
            sql += " where " + " and ".join(where)
        sql += " order by id desc limit ?"
        args.append(max(1, a.limit))
        rows = c.execute(sql, args).fetchall()

        if not rows:
            print("没有匹配的任务。")
            return 0

        total = c.execute("select count(*) from tasks").fetchone()[0]
        by_state = dict(c.execute(
            "select state, count(*) from tasks group by state"
        ).fetchall())
        print("tasks 共 %d 条：%s" % (
            total, "  ".join("%s=%d" % (k or "(空)", v) for k, v in sorted(by_state.items()))))
        print()

        for r in rows:
            keys = r.keys()
            pid = r["id"]
            state = (r["state"] or "-")
            mark = {"completed": "OK ", "failed": "ERR", "running": ".. ", "queued": ".. "}.get(state, "   ")
            print("%s #%-5s %-9s %-14s %-11s acct=%-4s %s -> %s" % (
                mark, pid, state,
                (r["model"] or "-")[:14],
                (r["size"] or "-")[:11],
                r["account_id"] if r["account_id"] is not None else "-",
                fmt_ts(r["created_at"]), fmt_ts(r["updated_at"])))
            if "file" in keys and r["file"]:
                print("        file: %s" % r["file"])
            if "prompt" in keys and r["prompt"]:
                p = r["prompt"] if a.prompt else r["prompt"].replace("\n", " ")[:40]
                print("        prompt: %s" % p)
            # error 只在失败时看，否则噪音太大
            if "error" in keys and r["error"] and (a.id or state == "failed"):
                print("        error: %s" % r["error"])
            if "retries" in keys and r["retries"]:
                ek = r["error_kind"] if "error_kind" in keys else "-"
                print("        retries: %s  error_kind: %s" % (r["retries"], ek))
            if a.id:
                for k in keys:
                    if k in ("id", "state", "model", "size", "account_id", "created_at",
                             "updated_at", "file", "prompt", "error", "retries", "error_kind"):
                        continue
                    print("        %s: %s" % (k, r[k]))
        return 0
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
