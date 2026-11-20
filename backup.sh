#!/system/bin/sh
# 备份：sqlite 用官方 backup API 热拷贝（WAL 模式下直接 cp 会丢数据），
# 连同 config.json / models.json 一起打包，默认保留最近 7 份。
DIR=$(cd "$(dirname "$0")" && pwd)
cd "$DIR" || exit 1
KEEP=${1:-7}
mkdir -p backups
STAMP=$(date +%Y%m%d-%H%M%S)
OUT="backups/marvis-$STAMP.tar.gz"
TMP=$(mktemp -d 2>/dev/null || echo "/tmp/mvbackup.$$")
mkdir -p "$TMP"

python3 - "$TMP" <<'PY'
import sqlite3, sys, os
dst = os.path.join(sys.argv[1], "marvis.db")
src = sqlite3.connect("data/marvis.db")
out = sqlite3.connect(dst)
with out:
    src.backup(out)
out.close(); src.close()
print("db ->", dst, os.path.getsize(dst), "B")
PY

cp -f config.json models.json "$TMP"/ 2>/dev/null
tar -czf "$OUT" -C "$TMP" . 2>/dev/null
rm -rf "$TMP"
if [ -f "$OUT" ]; then
  echo "备份完成: $OUT ($(wc -c < "$OUT") B)"
else
  echo "备份失败"; exit 1
fi

# 只留最近 $KEEP 份
ls -1t backups/marvis-*.tar.gz 2>/dev/null | tail -n +$((KEEP+1)) | while read -r f; do rm -f "$f"; done
echo "现有备份:"; ls -lh backups/ 2>/dev/null | tail -n +2
