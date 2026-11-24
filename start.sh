#!/usr/bin/env bash
# Launch the openP6 web app (binds 0.0.0.0:8080)
cd "$(dirname "$0")"
export OPENP6_PASSWORD="$(cat .password)"
export OPENP6_PORT="${OPENP6_PORT:-8080}"
exec .venv/bin/python web/server.py
