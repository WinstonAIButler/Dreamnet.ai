#!/usr/bin/env bash
# Dreamnet launcher for macOS and Linux: run ./start-mac-linux.sh (or double-click
# start-mac.command on a Mac), then use the browser tab it opens.
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "Setting up Dreamnet for the first time..."
  python3 -m venv .venv
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
