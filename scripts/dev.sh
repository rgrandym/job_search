#!/usr/bin/env bash
# Start (or restart) the AI Job Search web app: FastAPI backend + Vite frontend.
#
#   bash scripts/dev.sh              # restart both, stream logs, Ctrl-C stops both
#   bash scripts/dev.sh --stop       # stop both and exit
#   bash scripts/dev.sh --install    # force re-install of Python and npm dependencies
#   bash scripts/dev.sh --no-update  # skip the Claude Code / Codex CLI version check
#
# Every run first stops whatever is listening on the backend/frontend ports (including a
# previous run of this script), so there are never two servers fighting over a port.
# The Claude Code and Codex CLIs (the "claude_code" / "codex" model providers) are updated
# when older than the latest release, since new models need recent CLIs; offline, skipped.
# Env overrides: CONDA_ENV (job_search), BACKEND_PORT (8000), FRONTEND_PORT (5173).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONDA_ENV="${CONDA_ENV:-job_search}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
RUN_DIR="$ROOT/.run"
LOG_DIR="$RUN_DIR/logs"
FORCE_INSTALL=0
STOP_ONLY=0
CLI_UPDATE=1

for arg in "$@"; do
  case "$arg" in
    --install) FORCE_INSTALL=1 ;;
    --stop) STOP_ONLY=1 ;;
    --no-update) CLI_UPDATE=0 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

log() { printf '\033[1;34m[dev]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[dev]\033[0m %s\n' "$*" >&2; }

# ------------------------------------------------------------------ stop

kill_tree() {  # kill a pid and its children (uvicorn --reload / npm spawn children)
  local pid="$1" child
  for child in $(pgrep -P "$pid" 2>/dev/null || true); do kill_tree "$child"; done
  kill "$pid" 2>/dev/null || true
}

free_port() {
  local port="$1" name="$2" pids
  pids="$(lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -z "$pids" ]]; then return 0; fi
  log "Stopping $name on port $port (pid $(echo $pids | tr '\n' ' '))"
  for pid in $pids; do kill_tree "$pid"; done
  for _ in {1..20}; do  # wait up to 5s for a graceful exit
    lsof -ti tcp:"$port" -sTCP:LISTEN >/dev/null 2>&1 || return 0
    sleep 0.25
  done
  warn "$name did not exit; forcing"
  lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null | xargs kill -9 2>/dev/null || true
}

stop_all() {
  for f in "$RUN_DIR"/backend.pid "$RUN_DIR"/frontend.pid; do
    if [[ -f "$f" ]]; then
      kill_tree "$(cat "$f")"
      rm -f "$f"
    fi
  done
  free_port "$BACKEND_PORT" "backend"
  free_port "$FRONTEND_PORT" "frontend"
}

# A newer run takes ownership; an older run that notices this exits without touching ports.
mkdir -p "$RUN_DIR"
OWNER_FILE="$RUN_DIR/owner"
prev="$(cat "$OWNER_FILE" 2>/dev/null || true)"
echo "$$" > "$OWNER_FILE"  # claim ownership first, so the old run's cleanup stands down
if [[ -n "$prev" && "$prev" != "$$" ]] && kill -0 "$prev" 2>/dev/null; then
  log "Stopping previous dev.sh (pid $prev)"
  kill "$prev" 2>/dev/null || true
fi
i_own() { [[ "$(cat "$OWNER_FILE" 2>/dev/null)" == "$$" ]]; }

stop_all
if [[ "$STOP_ONLY" == 1 ]]; then rm -f "$OWNER_FILE"; log "Stopped."; exit 0; fi

# ------------------------------------------------------------------ conda env

CONDA_BASE="$(conda info --base 2>/dev/null || true)"
if [[ -z "$CONDA_BASE" ]]; then
  for c in "$HOME/miniconda3" "$HOME/anaconda3" /opt/miniconda3 /opt/anaconda3 /opt/homebrew/Caskroom/miniconda/base; do
    [[ -f "$c/etc/profile.d/conda.sh" ]] && CONDA_BASE="$c" && break
  done
fi
if [[ -z "$CONDA_BASE" ]]; then warn "conda not found. Install Miniconda first."; exit 1; fi
set +u  # conda's activation scripts reference unset variables
# shellcheck disable=SC1091
source "$CONDA_BASE/etc/profile.d/conda.sh"

if ! conda env list | awk '{print $1}' | grep -qx "$CONDA_ENV"; then
  log "Creating conda env '$CONDA_ENV' (python 3.12)"
  conda create -y -q -n "$CONDA_ENV" python=3.12 >/dev/null
  FORCE_INSTALL=1
fi
conda activate "$CONDA_ENV"
set -u
log "Conda env: $CONDA_ENV ($(python --version))"

cd "$ROOT"
if [[ "$FORCE_INSTALL" == 1 ]] || ! python -c "import fastapi, uvicorn, src" >/dev/null 2>&1; then
  log "Installing Python dependencies"
  pip install -q -e ".[dev]"
fi

# ------------------------------------------------------------------ frontend deps

command -v npm >/dev/null || { warn "npm not found. Install Node.js 20+."; exit 1; }
if [[ "$FORCE_INSTALL" == 1 || ! -d web/node_modules ]] || [[ web/package.json -nt web/node_modules ]]; then
  log "Installing npm dependencies"
  (cd web && npm install --silent)
fi

# ------------------------------------------------------------------ model CLIs

# The "claude_code" and "codex" providers run these CLIs, and new models need recent ones.
# Each is found the way the backend finds it (same PATH, same fallbacks) and updated through
# the install it came from when it is older than the latest release.
version_of() { "$1" --version 2>/dev/null | grep -Eo '[0-9]+(\.[0-9]+)+' | head -1; }
older_than() { python -c "import sys; v = lambda s: tuple(int(p) for p in s.split('.')); sys.exit(v(sys.argv[1]) >= v(sys.argv[2]))" "$1" "$2"; }

update_cli() {  # label binary npm-package cask
  local label="$1" bin="$2" package="$3" cask="$4" real current latest
  [[ -z "$bin" ]] && return 0  # optional provider; Settings explains how to install it
  current="$(version_of "$bin")"
  latest="$(npm view "$package" version --fetch-timeout=5000 --fetch-retries=0 2>/dev/null || true)"
  if [[ -z "$current" || -z "$latest" ]]; then
    warn "Could not check the $label version (offline?); using $bin ${current:-unknown}"
    return 0
  fi
  if ! older_than "$current" "$latest"; then
    log "$label: $current (up to date)"
    return 0
  fi
  log "Updating $label $current → $latest ($bin)"
  real="$(python -c "import os, sys; print(os.path.realpath(sys.argv[1]))" "$bin")"
  if [[ "$real" == */lib/node_modules/"$package"/* ]]; then
    # Install into this copy's own npm prefix: a bare `npm -g` or `claude update` may update
    # another copy (e.g. one in a different conda env) and leave this one old.
    npm install -g --silent --prefix "${real%%/lib/node_modules/*}" "$package@latest"
  elif [[ "$real" == */Caskroom/"$cask"/* ]]; then
    brew upgrade --cask "$cask"
  elif [[ "$real" == */extensions/openai.chatgpt-* ]] && command -v code >/dev/null; then
    code --install-extension openai.chatgpt --force >/dev/null  # the bundled CLI comes with it
  elif [[ "$label" == "Claude Code CLI" ]]; then
    "$bin" update
  else
    false
  fi || { warn "$label could not be updated here; update it by hand"; return 0; }
  log "$label: $(version_of "$(command -v "$(basename "$bin")" || echo "$bin")")"
}

codex_bin() {  # as src/core/llm/codex_backend.codex_binary: PATH, newest extension, the app
  command -v codex && return
  # shellcheck disable=SC2012
  ls -t "$HOME"/.vscode/extensions/openai.chatgpt-*/bin/*/codex \
    "$HOME"/.cursor/extensions/openai.chatgpt-*/bin/*/codex \
    /Applications/Codex.app/Contents/Resources/codex 2>/dev/null | head -1
}

if [[ "$CLI_UPDATE" == 1 ]]; then
  update_cli "Claude Code CLI" "$(command -v claude || true)" "@anthropic-ai/claude-code" "claude-code"
  update_cli "Codex CLI" "$(codex_bin || true)" "@openai/codex" "codex"
fi

[[ -f .env ]] || warn "No .env file. Set API keys in the app's Settings, or copy .env.example to .env"

# ------------------------------------------------------------------ start

mkdir -p "$LOG_DIR"
: > "$LOG_DIR/backend.log"
: > "$LOG_DIR/frontend.log"

log "Starting backend  → http://localhost:$BACKEND_PORT"
python -m uvicorn src.web.app:app --reload --reload-dir src --port "$BACKEND_PORT" \
  >"$LOG_DIR/backend.log" 2>&1 &
BACKEND_PID=$!
disown "$BACKEND_PID"  # no "Terminated" job notices on shutdown
echo "$BACKEND_PID" > "$RUN_DIR/backend.pid"

log "Starting frontend → http://localhost:$FRONTEND_PORT"
(cd web && BACKEND_PORT="$BACKEND_PORT" FRONTEND_PORT="$FRONTEND_PORT" exec npx vite --port "$FRONTEND_PORT" --strictPort) \
  >"$LOG_DIR/frontend.log" 2>&1 &
FRONTEND_PID=$!
disown "$FRONTEND_PID"
echo "$FRONTEND_PID" > "$RUN_DIR/frontend.pid"

TAIL_PIDS=""
cleanup() {
  trap - INT TERM EXIT
  [[ -n "$TAIL_PIDS" ]] && kill $TAIL_PIDS 2>/dev/null
  echo
  if i_own; then
    log "Shutting down"
    stop_all
    rm -f "$OWNER_FILE"
  else
    log "Superseded by a newer dev.sh run; leaving its servers running"
  fi
}
trap cleanup INT TERM EXIT

wait_for() {  # url name logfile
  for _ in {1..80}; do
    curl -fs -o /dev/null "$1" && { log "$2 ready"; return 0; }
    sleep 0.25
  done
  warn "$2 did not start within 20s. Last log lines:"
  tail -n 20 "$3" >&2
  return 1
}
wait_for "http://localhost:$BACKEND_PORT/api/state" "Backend" "$LOG_DIR/backend.log"
wait_for "http://localhost:$FRONTEND_PORT/" "Frontend" "$LOG_DIR/frontend.log"

log "App: http://localhost:$FRONTEND_PORT   (API docs: http://localhost:$BACKEND_PORT/docs)"
log "Logs: $LOG_DIR. Press Ctrl-C to stop both."
if [[ "${OPEN_BROWSER:-1}" == 1 ]] && command -v open >/dev/null; then
  if [[ "$(uname -s)" == Darwin ]]; then
    open -a "Google Chrome" "http://localhost:$FRONTEND_PORT" || open "http://localhost:$FRONTEND_PORT" || true
  else
    open "http://localhost:$FRONTEND_PORT" || true
  fi
fi

# Stream both logs with a prefix; exit (and clean up) if either server dies.
tail -n 0 -F "$LOG_DIR/backend.log" | sed -u 's/^/[api] /' &
TAIL_PIDS="$!"
tail -n 0 -F "$LOG_DIR/frontend.log" | sed -u 's/^/[web] /' &
TAIL_PIDS="$TAIL_PIDS $!"
while i_own && kill -0 "$BACKEND_PID" 2>/dev/null && kill -0 "$FRONTEND_PID" 2>/dev/null; do
  sleep 1
done
i_own && warn "A server exited. See the logs above."
exit 0
