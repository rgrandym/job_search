# Ports of the two ways to run the app, kept apart from each other and from the usual
# development ports (8000, 5173, 3000...) so that other programs never clash with them.
#   Dock app (scripts/desktop.sh, run by "Job Search.app")         18420
#   Browser web app (scripts/start.sh; dev.sh's backend)           18410
#   dev.sh's Vite UI                                               18411
# Sourced by the launchers; each may be overridden from the environment.

: "${DESKTOP_PORT:=18420}"
: "${WEB_PORT:=18410}"
: "${WEB_UI_PORT:=18411}"

# Export NAME=VALUE unless NAME is set in the environment or in .env (a line `NAME=...`).
default_env() {
  local name="$1" value="$2"
  [[ -n "${!name:-}" ]] && return 0
  grep -Eq "^[[:space:]]*(export[[:space:]]+)?$name=" "$ROOT/.env" 2>/dev/null && return 0
  export "$name=$value"
}

# Who is listening on a port, for messages: "pid 123: python -m uvicorn ...".
port_owner() {
  local pid
  pid="$(lsof -nP -t -iTCP:"$1" -sTCP:LISTEN 2>/dev/null | head -n 1)"
  [[ -n "$pid" ]] || return 1
  printf 'pid %s: %s' "$pid" "$(ps -o command= -p "$pid" 2>/dev/null | cut -c1-120)"
}
