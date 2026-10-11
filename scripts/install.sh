#!/usr/bin/env bash
# Set up the app on a computer: the conda env, Python and npm dependencies, the built UI, and
# on macOS the Dock app ("Job Search" in ~/Applications).
#
#   git clone https://github.com/rgrandym/job_search.git && cd job_search && bash scripts/install.sh
#   bash scripts/install.sh --no-app   # skip the Dock app (start with scripts/start.sh instead)
#
# Safe to run again (after `git pull`, or to rebuild the Dock app). It never touches data/,
# output/ or .env, where your CVs, settings and documents live.
# Env overrides: CONDA_ENV (job_search), BACKEND_PORT (8000).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONDA_ENV="${CONDA_ENV:-job_search}"
MAKE_APP=1

log() { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[install]\033[0m %s\n' "$*" >&2; }

for arg in "$@"; do
  case "$arg" in
    --no-app) MAKE_APP=0 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done
[[ "$(uname)" == Darwin ]] || MAKE_APP=0

# shellcheck source=lib/conda.sh
source "$ROOT/scripts/lib/conda.sh"

missing=0
if [[ -z "$(find_conda_base)" ]]; then
  warn "conda is missing: install Miniconda (https://www.anaconda.com/docs/getting-started/miniconda/main)"
  missing=1
fi
if ! command -v npm >/dev/null; then
  warn "npm is missing: install Node.js 20 or newer (https://nodejs.org/)"
  missing=1
elif (($(node -p 'process.versions.node.split(".")[0]') < 20)); then
  warn "Node.js $(node --version) is too old: install Node.js 20 or newer (https://nodejs.org/)"
  missing=1
fi
if [[ "$MAKE_APP" == 1 ]] && ! command -v swiftc >/dev/null; then
  warn "The Dock app needs Apple's command line tools: run xcode-select --install"
  missing=1
fi
if [[ "$missing" == 1 ]]; then warn "Install the above, open a new terminal and run this again."; exit 1; fi

activate_conda "$CONDA_ENV"
cd "$ROOT"
mkdir -p .run

log "Installing Python dependencies"
pip install -q -e .
touch .run/python.installed  # scripts/start.sh reinstalls only when pyproject.toml changes

log "Installing npm dependencies"
(cd web && npm ci --silent)
log "Building the UI"
(cd web && npm run build --silent)

[[ -f .env ]] || log "No .env file: add API keys in the app's Settings (or copy .env.example)."
[[ -d "/Applications/Google Chrome.app" ]] \
  || log "Optional: Google Chrome lets the app read job postings that need a browser."

if [[ "$MAKE_APP" == 1 ]]; then
  bash "$ROOT/scripts/macos/make_app.sh"
  open "$HOME/Applications/Job Search.app"
else
  log "Done. Start the app with: bash scripts/start.sh  → http://localhost:${BACKEND_PORT:-8000}"
fi
