#!/bin/sh
# 控制 stats_server.py：./server.sh {start|stop|restart|status} [服务参数，如 --port 18764]
# 须经本脚本启动，stop/restart 才能定位进程；PID 与日志存于 .stats/（派生数据，可删）。
# start/status 在服务未就绪（索引构建中，HTTP 尚未监听）时阻塞到就绪。
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

# 从参数列表解析访问地址，口径同 stats_server.py 的启动打印
parse_url() {
  port=18763
  host=127.0.0.1
  while [ $# -gt 0 ]; do
    case $1 in
      --port) port=${2:-18763}; shift 2 ;;
      --host) host=${2:-127.0.0.1}; shift 2 ;;
      *) shift ;;
    esac
  done
  case $host in
    0.0.0.0|::) host=127.0.0.1 ;;
    *:*) host="[$host]" ;;
  esac
  printf 'http://%s:%s\n' "$host" "$port"
}

# 阻塞直到 /api/meta 就绪（索引构建期间 HTTP 尚未监听）；等待期间进程退出返回 1
wait_ready() {
  url=$1
  pid=$2
  notified=0
  while ! curl -fsS -o /dev/null --max-time 2 "$url/api/meta" 2>/dev/null; do
    if [ "$notified" -eq 0 ]; then
      echo "索引构建中，等待就绪…"
      notified=1
    fi
    kill -0 "$pid" 2>/dev/null || return 1
    sleep 1
  done
}

start() {
  if pid=$(server_pid); then
    url=$(parse_url $(ps -p "$pid" -o command=))
    echo "已在运行 (PID $pid)，打开 $url"
    if ! wait_ready "$url" "$pid"; then
      echo "服务进程已退出" >&2
      return 1
    fi
    echo "状态: 运行中"
    return 0
  fi
  mkdir -p "$ROOT/.stats"
  rm -f "$PID_FILE"
  nohup "$ROOT/.venv/bin/python" "$ROOT/stats_server.py" "$@" >>"$LOG" 2>&1 &
  pid=$!
  printf '%s\n' "$pid" > "$PID_FILE"
  sleep 1
  if server_pid >/dev/null; then
    url=$(parse_url "$@")
    echo "已启动 (PID $pid)，打开 $url"
    if ! wait_ready "$url" "$pid"; then
      rm -f "$PID_FILE"
      echo "启动失败，最近日志：" >&2
      tail -n 5 "$LOG" >&2
      return 1
    fi
    echo "状态: 运行中，日志 $LOG"
  else
    rm -f "$PID_FILE"
    echo "启动失败，最近日志：" >&2
    tail -n 5 "$LOG" >&2
    return 1
  fi
}

status() {
  if pid=$(server_pid); then
    url=$(parse_url $(ps -p "$pid" -o command=))
    echo "运行中 (PID $pid)，日志 $LOG"
    if ! wait_ready "$url" "$pid"; then
      echo "服务进程已退出" >&2
      return 1
    fi
    echo "状态: 运行中"
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
