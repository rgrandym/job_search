#!/usr/bin/env bash
# The Dock app's own server (macOS "Job Search.app" runs this): the backend serving the built
# UI on the desktop port (18420, scripts/lib/ports.sh), apart from the browser web app
# (scripts/start.sh, dev.sh, port 18410), which the Dock app never starts or uses.
#
#   bash scripts/desktop.sh
#
# Everything else is scripts/start.sh: rebuilds what changed, reuses an up-to-date server of
# its own, replaces one running older code. The UI's settings are kept for this launcher
# (data/ui_state_desktop.json).
# Env overrides: DESKTOP_PORT (18420), CONDA_ENV (job_search).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/ports.sh
source "$ROOT/scripts/lib/ports.sh"

export BACKEND_PORT="$DESKTOP_PORT"
export JOBSEARCH_LAUNCHER=desktop
exec bash "$ROOT/scripts/start.sh"
