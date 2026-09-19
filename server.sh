#!/bin/sh
# 控制 stats_server.py：./server.sh {start|stop|restart|status} [服务参数，如 --port 18764]
# 须经本脚本启动，stop/restart 才能定位进程；PID 与日志存于 .stats/（派生数据，可删）。
set -eu

ROOT=$(cd "$(dirname "$0")" && pwd)
PID_FILE="$ROOT/.stats/server.pid"
LOG="$ROOT/.stats/server.log"

# 输出存活的服务 PID；无则返回 1
server_pid() {
  [ -f "$PID_FILE" ] || return 1
  pid=$(cat "$PID_FILE") || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  ps -p "$pid" -o command= | grep -q 'stats_server\.py' || return 1
  printf '%s\n' "$pid"
}

start() {
  if pid=$(server_pid); then
    echo "已在运行 (PID $pid)"
    return 0
  fi
  mkdir -p "$ROOT/.stats"
  rm -f "$PID_FILE"
  nohup "$ROOT/.venv/bin/python" "$ROOT/stats_server.py" "$@" >>"$LOG" 2>&1 &
  pid=$!
  printf '%s\n' "$pid" > "$PID_FILE"
  sleep 1
  if server_pid >/dev/null; then
    echo "已启动 (PID $pid)，日志 $LOG"
  else
    rm -f "$PID_FILE"
    echo "启动失败，最近日志：" >&2
    tail -n 5 "$LOG" >&2
    return 1
  fi
}

status() {
  if pid=$(server_pid); then
    echo "运行中 (PID $pid)，日志 $LOG"
  else
    echo "未在运行"
    return 1
  fi
}

stop() {
  pid=$(server_pid) || { rm -f "$PID_FILE"; echo "未在运行"; return 0; }
  kill "$pid"
  i=0
  while [ "$i" -lt 5 ] && kill -0 "$pid" 2>/dev/null; do
    sleep 1
    i=$((i + 1))
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -9 "$pid"
  fi
  rm -f "$PID_FILE"
  echo "已停止 (PID $pid)"
}

restart() {
  stop
  start "$@"
}

case ${1:-} in
  start)   shift; start "$@" ;;
  stop)    stop ;;
  restart) shift; restart "$@" ;;
  status)  status ;;
  *) echo "用法: $0 {start|stop|restart|status} [stats_server.py 参数]" >&2; exit 2 ;;
esac
