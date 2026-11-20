#!/system/bin/sh
# 马维斯反代 控制脚本
#   ./run.sh start|stop|restart|status|log|fg
# 只监听 127.0.0.1，不对外网开放。
DIR=$(cd "$(dirname "$0")" && pwd)
cd "$DIR" || exit 1

SELF="$DIR/$(basename "$0")"
PIDFILE="$DIR/data/serve.pid"
LOG="$DIR/logs/serve.log"
MAXLOG=5242880          # 5MB 轮转一次，只留一个 .1

mkdir -p data/files logs

port() {
  python3 -c "import json;print(json.load(open('config.json')).get('port',8088))" 2>/dev/null || echo 8088
}

# ── 出票 shim 目录：不写死，自动推导 + 自举拷贝 ─────────────────────────────
# 为什么不能只用项目内目录：/sdcard 是 FUSE(fuseblk)，.so 的 PT_LOAD 段没法 mmap 进去，
# dlopen 会报 "failed to map segment from shared object"。所以必须落到一个真实文件系统上。
# 顺序：$MARVIS_SHIM_DIR > config.json 的 shim_dir > /root/marvis-shim > $HOME/.marvis-shim
SHIM_SO="libqimei_patched.so libjni_stub.so libc.so libm.so libdl.so libandroid.so"

shim_dir() {
  if [ -n "$MARVIS_SHIM_DIR" ]; then echo "$MARVIS_SHIM_DIR"; return; fi
  CFG=$(python3 -c "import json;print((json.load(open('config.json')).get('shim_dir') or '').strip())" 2>/dev/null)
  if [ -n "$CFG" ]; then echo "$CFG"; return; fi
  if [ -w /root ] 2>/dev/null; then echo /root/marvis-shim; return; fi
  echo "${HOME:-/tmp}/.marvis-shim"
}

# 缺哪个补哪个；补完再验一遍。返回 0 = 可用。
ensure_shim() {
  D=$(shim_dir)
  SRC="$DIR/tools/bionic_shim"
  MISSING=""
  for f in $SHIM_SO; do
    [ -f "$D/$f" ] || MISSING="$MISSING $f"
  done
  if [ -n "$MISSING" ]; then
    mkdir -p "$D" 2>/dev/null
    echo "出票 shim 缺失，从 tools/bionic_shim 自举到 $D:$MISSING"
    for f in $MISSING; do
      if [ -f "$SRC/$f" ]; then
        cp -f "$SRC/$f" "$D/$f" 2>/dev/null || { echo "  ✗ 拷贝 $f 失败（目录不可写？）"; return 1; }
      else
        echo "  ✗ 源文件也不存在: $SRC/$f"; return 1
      fi
    done
    chmod 600 "$D"/*.so 2>/dev/null
  fi
  for f in $SHIM_SO; do
    [ -f "$D/$f" ] || { echo "  ✗ 自举后仍缺 $f"; return 1; }
  done
  MARVIS_SHIM_DIR="$D"
  export MARVIS_SHIM_DIR
  return 0
}

alive() {
  [ -f "$PIDFILE" ] || return 1
  PID=$(cat "$PIDFILE" 2>/dev/null)
  [ -n "$PID" ] || return 1
  kill -0 "$PID" 2>/dev/null || return 1
  # pid 可能被系统复用：必须核对命令行真的是 serve.py
  tr '\0' ' ' < "/proc/$PID/cmdline" 2>/dev/null | grep -q "serve.py"
}

rotate() {
  [ -f "$LOG" ] || return 0
  SIZE=$(wc -c < "$LOG" 2>/dev/null || echo 0)
  if [ "$SIZE" -gt "$MAXLOG" ]; then
    mv -f "$LOG" "$LOG.1"
  fi
}

wait_up() {
  P=$(port)
  i=0
  while [ $i -lt 40 ]; do
    if curl -s -m 1 "http://127.0.0.1:$P/health" >/dev/null 2>&1; then return 0; fi
    i=$((i+1)); sleep 0.5
  done
  return 1
}

case "$1" in
  start)
    if alive; then echo "已在运行 pid=$(cat "$PIDFILE")"; exit 0; fi
    if ! ensure_shim; then
      echo "出票模块不可用：对话请求会被上游 403（4100404）。"
      echo "修法：把 tools/bionic_shim/*.so 拷到一个真实文件系统目录，再 sh run.sh start"
      exit 1
    fi
    rotate
    setsid env LD_LIBRARY_PATH="$MARVIS_SHIM_DIR:$DIR/tools/bionic_shim:${LD_LIBRARY_PATH:-}" \
        nohup python3 -u serve.py >> "$LOG" 2>&1 &
    echo $! > "$PIDFILE"
    if wait_up; then
      echo "已启动 pid=$(cat "$PIDFILE")  http://127.0.0.1:$(port)  shim=$MARVIS_SHIM_DIR"
    else
      echo "启动失败，最后 20 行日志："
      tail -20 "$LOG"
      exit 1
    fi
    ;;
  stop)
    if ! alive; then echo "未在运行"; rm -f "$PIDFILE"; exit 0; fi
    PID=$(cat "$PIDFILE")
    kill "$PID" 2>/dev/null
    i=0
    while [ $i -lt 20 ] && kill -0 "$PID" 2>/dev/null; do i=$((i+1)); sleep 0.5; done
    kill -9 "$PID" 2>/dev/null
    rm -f "$PIDFILE"
    echo "已停止 pid=$PID"
    ;;
  restart)
    sh "$SELF" stop
    sleep 1
    sh "$SELF" start
    ;;
  status)
    if alive; then
      P=$(port)
      echo "运行中 pid=$(cat "$PIDFILE")  http://127.0.0.1:$P"
      curl -s -m 2 "http://127.0.0.1:$P/health" | python3 -m json.tool 2>/dev/null
    else
      echo "未运行"
    fi
    ;;
  log)
    tail -n "${2:-50}" "$LOG"
    ;;
  fg)
    rotate
    ensure_shim || echo "警告：出票模块不可用，对话会被 403"
    exec env LD_LIBRARY_PATH="$MARVIS_SHIM_DIR:$DIR/tools/bionic_shim:${LD_LIBRARY_PATH:-}" python3 -u serve.py
    ;;
  shim)
    D=$(shim_dir)
    echo "shim 目录: $D"
    for f in $SHIM_SO; do
      if [ -f "$D/$f" ]; then echo "  ✓ $f"; else echo "  ✗ $f（缺，start 时会自举）"; fi
    done
    echo "自举源  : $DIR/tools/bionic_shim"
    ;;
  autostart)
    # 把自启钩子写成「当前安装位置」，项目目录换了只要重跑这一条
    HOOK=/etc/profile.d/99-marvis-proxy.sh
    if [ ! -d /etc/profile.d ]; then echo "本机没有 /etc/profile.d，无法装自启钩子"; exit 1; fi
    cat > "$HOOK" <<EOF
# 容器启动 / 登录 shell 时自动拉起马维斯反代（幂等、后台、不阻塞）
# 由 marvis-proxy/run.sh autostart 生成，安装位置：$DIR
MP=$DIR
if [ -f "\$MP/watchdog.sh" ] && [ -d "\$MP/logs" ]; then
  WDPIDF="\$MP/logs/watchdog.pid"
  # 不信任 pid 文件：进程收尸期间 kill -0 仍返回成功、pid 会被复用、文件也可能丢失。
  # 直接扫 /proc 找真的 watchdog.sh，顺带把 pid 文件修正回真实值。
  ALIVE=0
  for d in /proc/[0-9]*; do
    [ -r "\$d/cmdline" ] || continue
    if tr '\\0' ' ' < "\$d/cmdline" 2>/dev/null | grep -q "watchdog\\.sh run"; then
      echo "\${d#/proc/}" > "\$WDPIDF" 2>/dev/null
      ALIVE=1
      break
    fi
  done
  if [ "\$ALIVE" = "0" ]; then
    ( cd "\$MP" && nohup sh watchdog.sh run >> logs/watchdog.log 2>&1 & echo \$! > "\$WDPIDF" ) >/dev/null 2>&1
  fi
fi
EOF
    chmod 644 "$HOOK" 2>/dev/null
    echo "自启钩子已写入 $HOOK → $DIR"
    ;;
  *)
    echo "用法: $0 start|stop|restart|status|log [行数]|fg|autostart|shim"
    exit 2
    ;;
esac
