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
  command -v osascript >/dev/null && osascript -e 'display dialog "Dreamnet needs Python first.\n\nYour browser will open python.org: click the yellow Download Python button, open the file and click Continue until it installs. Then double-click start-mac.command again." buttons {"OK"} default button 1 with title "Dreamnet"' >/dev/null 2>&1 || true
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
if [ ! -f .env ] && command -v osascript >/dev/null; then
  # On a Mac, ask for the two settings in popups instead of editing .env by hand.
  ask() {  # ask "question" hidden?
    osascript -e "text returned of (display dialog \"$1\" default answer \"\" $2 buttons {\"Cancel\", \"OK\"} default button 2 with title \"Dreamnet setup\")" 2>/dev/null
  }
  KEY=$(ask "Paste your Featherless API key:" "") || { echo "Setup cancelled."; exit 0; }
  PASS=$(ask "Choose a password for signing in to Dreamnet (your username is admin):" "with hidden answer") || { echo "Setup cancelled."; exit 0; }
  if [ -z "$KEY" ] || [ -z "$PASS" ]; then echo "Both are needed. Double-click start-mac.command to try again."; exit 0; fi
  cp .env.example .env
  KEY="$KEY" PASS="$PASS" python - <<'PY'
import os, re
p = ".env"; s = open(p).read()
s = re.sub(r"(?m)^FEATHERLESS_API_KEY=.*$", lambda m: "FEATHERLESS_API_KEY=" + os.environ["KEY"].strip(), s)
s = re.sub(r"(?m)^ADMIN_PASSWORD=.*$", lambda m: "ADMIN_PASSWORD=" + os.environ["PASS"].strip(), s)
open(p, "w").write(s)
PY
  osascript -e 'display dialog "All set. Dreamnet opens in your browser now.\n\nSign in with username admin and the password you just chose. Keep the Terminal window open while you use Dreamnet." buttons {"OK"} default button 1 with title "Dreamnet"' >/dev/null 2>&1 &
fi
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Add your Featherless key and a password to .env, save it, then run this again."
  (command -v open >/dev/null && open -e .env) || (command -v xdg-open >/dev/null && xdg-open .env) || true
  exit 0
fi
( sleep 2; (command -v open >/dev/null && open http://localhost:8080) || (command -v xdg-open >/dev/null && xdg-open http://localhost:8080) || true ) &
python -m app.main
