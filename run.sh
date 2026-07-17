#!/usr/bin/env bash
# UpTube 서버를 tmux 세션으로 실행한다.
# 사용법: ./run.sh [start|stop|restart|status|logs]
set -euo pipefail

SESSION="uptube"
PORT="${PORT:-8000}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

port_open() {
  python3 - "$PORT" <<'PY'
import socket, sys
s = socket.socket()
s.settimeout(1)
try:
    s.connect(("127.0.0.1", int(sys.argv[1])))
    sys.exit(0)
except OSError:
    sys.exit(1)
finally:
    s.close()
PY
}

start() {
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "이미 실행 중입니다. (tmux 세션: $SESSION)"
    exit 0
  fi
  tmux new-session -d -s "$SESSION" -c "$DIR" "PORT=$PORT python3 app.py 2>&1 | tee -a uptube.log"

  # 서버가 실제로 뜰 때까지 최대 10초 대기, 실패 시 로그를 보여준다
  for _ in $(seq 1 10); do
    if port_open; then
      echo "UpTube 시작됨: http://localhost:$PORT (tmux 세션: $SESSION)"
      echo "로그 보기: ./run.sh logs  /  세션 접속: tmux attach -t $SESSION"
      return 0
    fi
    sleep 1
  done

  echo "!! 서버가 시작되지 않았습니다. 최근 로그:"
  echo "----------------------------------------"
  tail -n 30 "$DIR/uptube.log" 2>/dev/null || echo "(로그 없음)"
  echo "----------------------------------------"
  echo "직접 실행해 오류를 확인하세요: python3 app.py"
  exit 1
}

stop() {
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    tmux kill-session -t "$SESSION"
    echo "UpTube 중지됨."
  else
    echo "실행 중인 세션이 없습니다."
  fi
}

status() {
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "실행 중 (tmux 세션: $SESSION, 포트: $PORT)"
  else
    echo "중지됨"
  fi
}

logs() {
  tail -n 50 -f "$DIR/uptube.log"
}

case "${1:-start}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; start ;;
  status)  status ;;
  logs)    logs ;;
  *) echo "사용법: $0 [start|stop|restart|status|logs]"; exit 1 ;;
esac
