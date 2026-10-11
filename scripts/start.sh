#!/usr/bin/env bash
# Run the app as one server: the backend serving the built UI (web/dist), no reload, no Vite.
# The Dock app (scripts/macos/) runs this; it also works from a terminal → http://localhost:8000
#
#   bash scripts/start.sh
#
# What changed since the last run (e.g. after `git pull`) is rebuilt first: Python dependencies
# when pyproject.toml changed, the UI when web/ changed. If an up-to-date backend is already up,
# it exits; one running older code (its /api/health says "stale") is stopped and replaced, so
# reopening the Dock app always runs the latest code. A dev backend (`--reload`) is kept: it
# reloads itself, and only the UI is rebuilt.
# Env overrides: CONDA_ENV (job_search), BACKEND_PORT (8000).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONDA_ENV="${CONDA_ENV:-job_search}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
RUN_DIR="$ROOT/.run"

log() { printf '[start] %s\n' "$*"; }
warn() { printf '[start] %s\n' "$*" >&2; }

KEEP_RUNNING=0
stop_stale_backend() {  # the out-of-date backend on our port, if it is ours and not a dev one
  local pid cmd
  pid="$(lsof -nP -t -iTCP:"$BACKEND_PORT" -sTCP:LISTEN 2>/dev/null | head -n 1)"
  [[ -n "$pid" ]] || return 1
  cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
  [[ "$cmd" == *src.web.app* && "$cmd" != *--reload* ]] || return 1
  log "Replacing the backend running older code (pid $pid)"
  kill "$pid" 2>/dev/null || return 1
  for _ in $(seq 1 40); do  # up to 10 s for it to let go of the port
    lsof -nP -t -iTCP:"$BACKEND_PORT" -sTCP:LISTEN >/dev/null 2>&1 || return 0
    sleep 0.25
  done
  kill -9 "$pid" 2>/dev/null || true
}

if health="$(curl -fsS --max-time 2 "http://127.0.0.1:$BACKEND_PORT/api/health" 2>/dev/null)"; then
  if [[ "$health" != *'"stale":true'* ]]; then
    log "Already running → http://localhost:$BACKEND_PORT"
    exit 0
  fi
  stop_stale_backend || KEEP_RUNNING=1
fi

# shellcheck source=lib/conda.sh
source "$ROOT/scripts/lib/conda.sh"
activate_conda "$CONDA_ENV"
cd "$ROOT"
mkdir -p "$RUN_DIR"

if [[ "$CONDA_CREATED" == 1 || pyproject.toml -nt "$RUN_DIR/python.installed" ]] \
  || ! python -c "import fastapi, uvicorn, src" >/dev/null 2>&1; then
  log "Installing Python dependencies"
  pip install -q -e .
  touch "$RUN_DIR/python.installed"
fi

ui_stale() {  # no build yet, or a UI source newer than it
  [[ -f web/dist/index.html ]] || return 0
  [[ -n "$(find web/src web/index.html web/package.json web/package-lock.json web/vite.config.ts \
    web/tailwind.config.js web/postcss.config.js -newer web/dist/index.html -print -quit \
    2>/dev/null)" ]]
}

if ui_stale; then
  if ! command -v npm >/dev/null; then
    warn "npm not found, so the UI cannot be rebuilt. Install Node.js 20+."
    [[ -f web/dist/index.html ]] || exit 1
  else
    if [[ ! -d web/node_modules || web/package.json -nt web/node_modules ]]; then
      log "Installing npm dependencies"
      (cd web && npm install --silent)
    fi
    log "Building the UI"
    (cd web && npm run build --silent)
  fi
fi

if [[ "$KEEP_RUNNING" == 1 ]]; then
  log "Already running (it reloads itself) → http://localhost:$BACKEND_PORT"
  exit 0
fi

log "Serving → http://localhost:$BACKEND_PORT"
# The open chat WebSocket would otherwise hold up quitting the app.
exec python -m uvicorn src.web.app:app --host 127.0.0.1 --port "$BACKEND_PORT" \
  --timeout-graceful-shutdown 3
