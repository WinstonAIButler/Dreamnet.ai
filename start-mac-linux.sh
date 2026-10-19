#!/usr/bin/env bash
# Dreamnet launcher for macOS and Linux: run ./start-mac-linux.sh (or double-click
# start-mac.command on a Mac), then use the browser tab it opens.
set -e
cd "$(dirname "$0")"
# Dreamnet needs Python 3.10 or newer; macOS's built-in python3 can be older.
PY=""
for p in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$p" >/dev/null && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then PY="$p"; break; fi
done
if [ -z "$PY" ]; then
  echo "Dreamnet needs Python 3.10 or newer. Install it from https://www.python.org/downloads/ and run this again."
  (command -v open >/dev/null && open https://www.python.org/downloads/) || true
  exit 1
fi
if [ -d .venv ] && ! .venv/bin/python -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
  rm -rf .venv  # made with an older Python
fi
if [ ! -d .venv ]; then
  echo "Setting up Dreamnet for the first time (this takes a minute)..."
  "$PY" -m venv .venv
fi
. .venv/bin/activate
python -m pip install -q -r requirements.txt
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Add your Featherless key and a password to .env, save it, then run this again."
  (command -v open >/dev/null && open -e .env) || (command -v xdg-open >/dev/null && xdg-open .env) || true
  exit 0
fi
( sleep 2; (command -v open >/dev/null && open http://localhost:8080) || (command -v xdg-open >/dev/null && xdg-open http://localhost:8080) || true ) &
python -m app.main
