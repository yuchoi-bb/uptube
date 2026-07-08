#!/usr/bin/env bash
# UpTube 서버를 tmux 세션으로 실행한다.
# 사용법: ./run.sh [start|stop|restart|status|logs]
set -euo pipefail

SESSION="uptube"
PORT="${PORT:-8000}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

start() {
  if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "이미 실행 중입니다. (tmux 세션: $SESSION)"
    exit 0
  fi
  tmux new-session -d -s "$SESSION" -c "$DIR" "PORT=$PORT python3 app.py 2>&1 | tee -a uptube.log"
  echo "UpTube 시작됨: http://localhost:$PORT (tmux 세션: $SESSION)"
  echo "로그 보기: ./run.sh logs  /  세션 접속: tmux attach -t $SESSION"
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
