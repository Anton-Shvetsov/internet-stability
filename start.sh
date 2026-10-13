#!/usr/bin/env bash
# macOS / Linux: starts the monitor with the Python that install.sh put into runtime/.
cd "$(dirname "$0")"
if [ ! -x runtime/venv/bin/python ]; then
  echo "The monitor is not installed. Run install.sh first (see README.md)."
  exit 1
fi
# supervisor.py runs app.py and keeps it updated from GitHub (see README.md).
exec runtime/venv/bin/python supervisor.py
