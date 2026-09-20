#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

if command -v python >/dev/null 2>&1; then
  PYTHON=python
elif command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
else
  echo "错误：没有找到 Python。"
  echo "Termux 请先执行：pkg install python"
  exit 1
fi

HOST="${GATEWAY_HOST:-127.0.0.1}"
PORT="${GATEWAY_PORT:-8787}"

exec "$PYTHON" app.py --host "$HOST" --port "$PORT" "$@"
