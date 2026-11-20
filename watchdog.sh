#!/system/bin/sh
# 马维斯反代 守护脚本
#   sh watchdog.sh start|stop|status|run
#
# 守护什么：serve.py 挂了立刻拉起；进程在但 /health 连续探不通也重启。
# 不依赖 systemd / cron（本机都没有），就是一个死循环 + sleep。
DIR=$(cd "$(dirname "$0")" && pwd)
cd "$DIR" || exit 1

SELF="$DIR/$(basename "$0")"
WDPID="$DIR/logs/watchdog.pid"
WLOG="$DIR/logs/watchdog.log"
SERVEPID="$DIR/data/serve.pid"
INTERVAL=${WATCHDOG_INTERVAL:-15}
FAIL_LIMIT=${WATCHDOG_FAIL_LIMIT:-3}

mkdir -p logs data

port() {
  python3 -c "import json;print(json.load(open('config.json')).get('port',8088))" 2>/dev/null || echo 8088
}

log() {
  printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$WLOG"
}

wd_alive() {
  [ -f "$WDPID" ] || return 1
  P=$(cat "$WDPID" 2>/dev/null)
  [ -n "$P" ] || return 1
  kill -0 "$P" 2>/dev/null || return 1
  tr '\0' ' ' < "/proc/$P/cmdline" 2>/dev/null | grep -q "watchdog.sh"
}

healthy() {
  P=$(port)
  curl -s -m 3 "http://127.0.0.1:$P/health" 2>/dev/null | grep -q '"ok"'
}

run_loop() {
  FAILS=0
  log "守护启动 pid=$$ 间隔=${INTERVAL}s 探活阈值=${FAIL_LIMIT}"
  while :; do
    SPID=$(cat "$SERVEPID" 2>/dev/null)
    if [ -z "$SPID" ] || ! kill -0 "$SPID" 2>/dev/null || ! tr '\0' ' ' < "/proc/$SPID/cmdline" 2>/dev/null | grep -q serve.py; then
      log "服务未运行（pid=${SPID:-无}）→ 拉起"
      sh "$DIR/run.sh" start >> "$WLOG" 2>&1
      FAILS=0
    elif healthy; then
      FAILS=0
    else
      FAILS=$((FAILS + 1))
      log "健康检查失败 第 ${FAILS}/${FAIL_LIMIT} 次（pid=$SPID）"
      if [ "$FAILS" -ge "$FAIL_LIMIT" ]; then
        log "连续不健康 → 重启"
        sh "$DIR/run.sh" stop >> "$WLOG" 2>&1
        sleep 1
        sh "$DIR/run.sh" start >> "$WLOG" 2>&1
        FAILS=0
      fi
    fi
    sleep "$INTERVAL"
  done
}

case "$1" in
  start)
    if wd_alive; then echo "守护已在运行 pid=$(cat "$WDPID")"; exit 0; fi
    setsid nohup sh "$SELF" run >> "$WLOG" 2>&1 &
    echo $! > "$WDPID"
    sleep 1
    if wd_alive; then echo "守护已启动 pid=$(cat "$WDPID")"; else echo "守护启动失败，见 $WLOG"; exit 1; fi
    ;;
  stop)
    if ! wd_alive; then echo "守护未在运行"; rm -f "$WDPID"; exit 0; fi
    P=$(cat "$WDPID")
    kill "$P" 2>/dev/null
    i=0
    while [ $i -lt 10 ] && kill -0 "$P" 2>/dev/null; do i=$((i+1)); sleep 0.5; done
    kill -9 "$P" 2>/dev/null
    rm -f "$WDPID"
    log "守护停止（命令）"
    echo "守护已停止"
    ;;
  status)
    if wd_alive; then
      echo "守护运行中 pid=$(cat "$WDPID")"
      tail -3 "$WLOG" 2>/dev/null
    else
      echo "守护未运行"
    fi
    sh "$DIR/run.sh" status
    ;;
  run)
    # 前台跑循环（供 nohup 用，也可以直接 sh watchdog.sh run 看输出）
    echo $$ > "$WDPID"
    trap 'log "守护收到信号，退出"; rm -f "$WDPID"; exit 0' INT TERM
    run_loop
    ;;
  *)
    echo "用法: sh watchdog.sh start|stop|status|run"
    exit 2
    ;;
esac
