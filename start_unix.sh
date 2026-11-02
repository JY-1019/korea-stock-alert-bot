#!/usr/bin/env sh
cd "$(dirname "$0")" || exit 1
[ -f config.json ] || cp config.example.json config.json
if [ ! -x .venv/bin/python ]; then
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
      "$candidate" -m venv .venv
      break
    fi
  done
fi
if [ ! -x .venv/bin/python ]; then
  echo "Python 3.11 이상을 설치하세요."
  exit 1
fi
.venv/bin/python -m pip install -q -e . || exit 1
exec .venv/bin/python -m market_watcher serve --config config.json
