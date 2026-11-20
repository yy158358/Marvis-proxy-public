# -*- coding: utf-8 -*-
"""热备份：sqlite 用官方 backup API 拷贝（WAL 下直接 cp 会缺数据），再连同配置打包。"""
import os
import sqlite3
import threading
import tarfile
import time


def _stamp(t=None):
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(t or time.time()))


def make_backup(ctx, keep=None):
    store, config = ctx["store"], ctx["config"]
    data_dir = ctx["data_dir"]
    out_dir = os.path.join(data_dir, "..", "backups")
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    keep = int(keep if keep is not None else config.get("backup_keep", 7))

    tmp_db = os.path.join(out_dir, ".snapshot-%d.db" % os.getpid())
    src = sqlite3.connect(store.path)
    dst = sqlite3.connect(tmp_db)
    try:
        src.backup(dst)          # 官方在线备份，一致快照
    finally:
        dst.close()
        src.close()

    name = "marvis-%s.tar.gz" % _stamp()
    path = os.path.join(out_dir, name)
    with tarfile.open(path, "w:gz") as tar:
        tar.add(tmp_db, arcname="marvis.db")
        for f in ("config.json", "models.json"):
            p = os.path.join(ctx["root"], f)
            if os.path.exists(p):
                tar.add(p, arcname=f)
    os.unlink(tmp_db)

    olds = sorted((f for f in os.listdir(out_dir) if f.startswith("marvis-") and f.endswith(".tar.gz")))
    removed = 0
    while len(olds) > keep:
        os.unlink(os.path.join(out_dir, olds.pop(0)))
        removed += 1
    return {"ok": True, "file": path, "size": os.path.getsize(path), "removed": removed, "keep": keep}


def start_backup_thread(ctx):
    """daemon 线程：启动时若当天没有备份就先做一份，之后每 backup_interval_hours 做一次。"""
    config = ctx["config"]
    hours = float(config.get("backup_interval_hours", 24) or 24)
    out_dir = os.path.abspath(os.path.join(ctx["data_dir"], "..", "backups"))
    last = {"at": 0.0}

    def _today_done():
        if not os.path.isdir(out_dir):
            return False
        day = time.strftime("%Y%m%d", time.localtime())
        return any(f.startswith("marvis-" + day) for f in os.listdir(out_dir))

    def loop():
        if not _today_done():
            try:
                ctx["last_backup"] = make_backup(ctx)
            except Exception as e:
                ctx["last_backup"] = {"ok": False, "error": str(e)}
        while True:
            time.sleep(max(60.0, hours * 3600.0))
            try:
                ctx["last_backup"] = make_backup(ctx)
            except Exception as e:
                ctx["last_backup"] = {"ok": False, "error": str(e)}

    t = threading.Thread(target=loop, name="backup", daemon=True)
    t.start()
    return t
