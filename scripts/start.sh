#!/usr/bin/env bash
# Run the app as one server: the backend serving the built UI (web/dist), no reload, no Vite.
# The Dock app (scripts/macos/) runs this; it also works from a terminal → http://localhost:8000
#
#   bash scripts/start.sh
#
# What changed since the last run (e.g. after `git pull`) is rebuilt first: Python dependencies
# when pyproject.toml changed, the UI when web/ changed. If the backend is already up, it exits.
# Env overrides: CONDA_ENV (job_search), BACKEND_PORT (8000).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONDA_ENV="${CONDA_ENV:-job_search}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
RUN_DIR="$ROOT/.run"

log() { printf '[start] %s\n' "$*"; }
warn() { printf '[start] %s\n' "$*" >&2; }

if curl -fsS --max-time 2 "http://127.0.0.1:$BACKEND_PORT/api/health" >/dev/null 2>&1; then
  log "Already running → http://localhost:$BACKEND_PORT"
  exit 0
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

log "Serving → http://localhost:$BACKEND_PORT"
# The open chat WebSocket would otherwise hold up quitting the app.
exec python -m uvicorn src.web.app:app --host 127.0.0.1 --port "$BACKEND_PORT" \
  --timeout-graceful-shutdown 3
