#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ $# -ne 1 ]]; then
  echo "用法：./restore.sh backups/gateway-日期.db"
  exit 1
fi
SOURCE="$1"
if [[ ! -f "$SOURCE" ]]; then
  echo "找不到备份文件：$SOURCE"
  exit 1
fi

if command -v pgrep >/dev/null 2>&1 && pgrep -f "[p]ython.*app.py" >/dev/null 2>&1; then
  echo "检测到网关可能正在运行。请先按 Ctrl+C 关闭，再执行恢复。"
  exit 1
fi

mkdir -p data
if [[ -f data/gateway.db ]]; then
  cp data/gateway.db "data/gateway.db.before-restore-$(date +%Y%m%d-%H%M%S)"
fi
cp "$SOURCE" data/gateway.db
rm -f data/gateway.db-wal data/gateway.db-shm
chmod 600 data/gateway.db
echo "恢复完成。现在可以执行 ./start.sh"
