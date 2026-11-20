# -*- coding: utf-8 -*-
"""命令行热备份 —— 等价于 POST /admin/backup，给 backup.bat 用（服务没跑时也能备）。

复用的是同一套逻辑：core.backup.make_backup()，即 sqlite 官方 backup API 扣一致快照
（WAL 下直接 cp marvis.db 会丢最近的写入），再连同 config.json / models.json 打 tar.gz。

用法：
    python tools/backup_now.py         # 保留份数取 config.json 的 backup_keep（默认 7）
    python tools/backup_now.py 14      # 保留 14 份
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import serve                      # noqa: E402  复用它的 config 解析（环境变量覆盖、相对路径归一）
from core.backup import make_backup   # noqa: E402


class _Store(object):
    """make_backup 只用到 store.path，这里给个最小替身，不必真的建 sqlite 连接。"""

    def __init__(self, path):
        self.path = path


def main(argv):
    keep = None
    for a in argv[1:]:
        if a.isdigit():
            keep = int(a)
    cfg = serve.load_config()
    data_dir = cfg["data_dir"]
    db = os.path.join(data_dir, "marvis.db")
    if not os.path.isfile(db):
        print("[ERR] 找不到数据库：%s" % db)
        return 2

    ctx = {"config": cfg, "data_dir": data_dir, "root": ROOT, "store": _Store(db)}
    res = make_backup(ctx, keep)
    print("备份完成：%s" % res["file"])
    print("大小 %.1f KB，保留 %d 份%s" % (res["size"] / 1024.0, res["keep"],
                                     "，清理旧备份 %d 份" % res["removed"] if res.get("removed") else ""))
    out_dir = os.path.dirname(res["file"])
    print("现有备份：")
    for f in sorted(os.listdir(out_dir), reverse=True):
        if f.startswith("marvis-") and f.endswith(".tar.gz"):
            print("  %-34s %9d B" % (f, os.path.getsize(os.path.join(out_dir, f))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
