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

    # 临时快照落在一级隐藏名上，整个流程包在 try/finally 里：
    # tar 打包中途失败（磁盘满、文件被占用、进程被杀）以前会把 .snapshot-<pid>.db
    # 永久留在 backups/，下一次备份不认它、清理轮也不扫它，越积越多。
    tmp_db = os.path.join(out_dir, ".snapshot-%d.db" % os.getpid())
    name = "marvis-%s.tar.gz" % _stamp()
    path = os.path.join(out_dir, name)
    try:
        src = sqlite3.connect(store.path)
        dst = sqlite3.connect(tmp_db)
        try:
            src.backup(dst)      # 官方在线备份，一致快照
        finally:
            dst.close()
            src.close()

        with tarfile.open(path, "w:gz") as tar:
            tar.add(tmp_db, arcname="marvis.db")
            for f in ("config.json", "models.json"):
                p = os.path.join(ctx["root"], f)
                if os.path.exists(p):
                    tar.add(p, arcname=f)
    except Exception:
        # 打了一半的归档留着比没有更糟：下次按名字排序会把它当成一份完整备份。
        for junk in (tmp_db, path):
            try:
                os.unlink(junk)
            except OSError:
                pass
        raise
    finally:
        try:
            os.unlink(tmp_db)
        except OSError:
            pass

    olds = sorted((f for f in os.listdir(out_dir) if f.startswith("marvis-") and f.endswith(".tar.gz")))
    removed = 0
    while len(olds) > keep:
        os.unlink(os.path.join(out_dir, olds.pop(0)))
        removed += 1
    return {"ok": True, "file": path, "size": os.path.getsize(path), "removed": removed, "keep": keep}


def sweep_stale_snapshots(out_dir=None, ctx=None, older_than=3600.0):
    """删掉 backups/ 下别人留下的 .snapshot-<pid>.db。

    正常流程自己会清理（make_backup 的 finally），这里的对象是**异常退出**遗留的：
    进程被 kill -9 / 关机 / 蓝屏，finally 没机会跑。判断依据是「不是当前进程的 pid
    且超过 older_than 秒没被写过」——只靠 mtime 不够，另一个并发的备份可能正在写。
    返回删掉的个数。
    """
    if out_dir is None:
        if ctx is None:
            return 0
        out_dir = os.path.abspath(os.path.join(ctx["data_dir"], "..", "backups"))
    if not os.path.isdir(out_dir):
        return 0
    mine = ".snapshot-%d.db" % os.getpid()
    cutoff = time.time() - float(older_than)
    removed = 0
    for f in os.listdir(out_dir):
        if not f.startswith(".snapshot-") or not f.endswith(".db") or f == mine:
            continue
        p = os.path.join(out_dir, f)
        try:
            if os.path.getmtime(p) > cutoff:
                continue
            os.unlink(p)
            removed += 1
        except OSError:
            pass
    return removed


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
        # 上来先扫一次上一次进程异常退出留下的快照，别等到第一次备份
        try:
            n = sweep_stale_snapshots(out_dir)
            if n:
                print("[backup] 清掉 %d 个上次残留的 .snapshot-*.db" % n)
        except Exception:
            pass
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
