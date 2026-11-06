#!/bin/zsh
set -e

PROJECT_DIR="${0:A:h}"
VENV_DIR="$PROJECT_DIR/.dashboard-venv"

cd "$PROJECT_DIR"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required. Install it from https://www.python.org/downloads/ and open this launcher again."
  read -k 1 "?Press any key to close."
  exit 1
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "Setting up the dashboard for the first time…"
  python3 -m venv "$VENV_DIR"
fi

echo "Checking dashboard dependencies…"
"$VENV_DIR/bin/python" -m pip install --quiet --disable-pip-version-check -r "$PROJECT_DIR/requirements.txt"

echo "Opening the dashboard updater…"
echo "Keep this window open while using the dashboard. Press Control-C here when finished."
exec "$VENV_DIR/bin/python" "$PROJECT_DIR/intake_server.py" --open
