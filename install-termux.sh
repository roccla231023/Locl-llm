#!/data/data/com.termux/files/usr/bin/bash
set -euo pipefail

printf '\n== 本地 LLM 网关：Termux 安装检查 ==\n\n'

if ! command -v pkg >/dev/null 2>&1; then
  echo "提示：当前环境似乎不是 Termux。"
  echo "本项目没有第三方 Python 依赖，只要安装 Python 3.11+ 即可。"
else
  echo "正在安装/确认 Python 与 CA 证书……"
  pkg update -y
  pkg install -y python ca-certificates
fi

if command -v python >/dev/null 2>&1; then
  PYTHON=python
elif command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
else
  echo "错误：Python 安装失败。"
  exit 1
fi

"$PYTHON" - <<'PY'
import sys, sqlite3, ssl
if sys.version_info < (3, 11):
    raise SystemExit("需要 Python 3.11 或更高版本")
print("Python:", sys.version.split()[0])
print("SQLite:", sqlite3.sqlite_version)
print("TLS: 可用")
PY

mkdir -p data backups
chmod 700 data backups
chmod +x start.sh backup.sh restore.sh

printf '\n安装检查完成。\n\n启动命令：\n  ./start.sh\n\n浏览器管理地址：\n  http://127.0.0.1:8787\n\n'
