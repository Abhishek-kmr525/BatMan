#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
PYBIN="${PYTHON:-/opt/homebrew/bin/python3.11}"
if [ ! -x "$PYBIN" ]; then PYBIN="python3"; fi
if [ ! -d .venv ]; then
  "$PYBIN" -m venv .venv
fi
source .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt
exec uvicorn app.main:app --reload --host 0.0.0.0 --port 4000
