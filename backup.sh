#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p backups
chmod 700 backups

DB="data/gateway.db"
if [[ ! -f "$DB" ]]; then
  echo "还没有数据库，无需备份。"
  exit 0
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="backups/gateway-$STAMP.db"

if command -v python >/dev/null 2>&1; then PYTHON=python; else PYTHON=python3; fi
"$PYTHON" - "$DB" "$DEST" <<'PY'
import sqlite3, sys
source, destination = sys.argv[1:]
with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
    src.backup(dst)
PY
chmod 600 "$DEST"
echo "备份完成：$DEST"
echo "注意：备份中包含真实上游 Key，请勿上传或分享。"
