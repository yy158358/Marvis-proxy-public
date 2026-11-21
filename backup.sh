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
# 临时目录无论成败都要清：中途被打断时留在 /tmp 里的整份数据库副本
# 既占空间，又是一份没人管的明文凭据。
trap 'rm -rf "$TMP"' EXIT INT TERM

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
# 先写临时名再改名：tar 写到一半失败时不会留下一个能被 ls 当成完整备份的半个归档
if ! tar -czf "$OUT.part" -C "$TMP" . 2>/dev/null; then
  rm -f "$OUT.part"
  echo "备份失败"; exit 1
fi
mv -f "$OUT.part" "$OUT"
[ -f "$OUT" ] || { echo "备份失败"; exit 1; }
echo "备份完成: $OUT ($(wc -c < "$OUT") B)"

# 只留最近 $KEEP 份
ls -1t backups/marvis-*.tar.gz 2>/dev/null | tail -n +$((KEEP+1)) | while read -r f; do rm -f "$f"; done
echo "现有备份:"; ls -lh backups/ 2>/dev/null | tail -n +2
