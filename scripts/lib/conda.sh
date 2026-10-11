# Shared by the scripts in scripts/: find conda, create the env if it is missing, activate it.
# Source it after defining log and warn. activate_conda sets CONDA_CREATED=1 for a new env.

find_conda_base() {
  local base c
  base="$(conda info --base 2>/dev/null || true)"
  if [[ -z "$base" ]]; then
    for c in "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/miniforge3" /opt/miniconda3 \
      /opt/anaconda3 /opt/homebrew/Caskroom/miniconda/base /opt/homebrew/Caskroom/miniforge/base; do
      [[ -f "$c/etc/profile.d/conda.sh" ]] && base="$c" && break
    done
  fi
  printf '%s' "$base"
}

activate_conda() {  # env-name
  local env="$1" base
  CONDA_CREATED=0
  base="$(find_conda_base)"
  if [[ -z "$base" ]]; then warn "conda not found. Install Miniconda first."; return 1; fi
  set +u  # conda's activation scripts reference unset variables
  # shellcheck disable=SC1091
  source "$base/etc/profile.d/conda.sh"
  if ! conda env list | awk '{print $1}' | grep -qx "$env"; then
    log "Creating conda env '$env' (python 3.12)"
    conda create -y -q -n "$env" python=3.12 >/dev/null
    CONDA_CREATED=1
  fi
  conda activate "$env"
  set -u
}
