#!/usr/bin/env bash
# macOS / Linux installer: downloads the project from GitHub and a standalone Python into
# ~/InternetStability, then installs the requirements into runtime/venv. Run it on its own:
#   curl -fsSL https://raw.githubusercontent.com/Anton-Shvetsov/internet-stability/main/install.sh | bash
# Running it again updates the code and the requirements; config.json, data/ and runtime/python stay.
set -euo pipefail

ROOT="$HOME/InternetStability"
REPO_URL="https://github.com/Anton-Shvetsov/internet-stability/archive/refs/heads/main.tar.gz"

# python-build-standalone: relocatable CPython builds (the ones uv uses); python.org has none for macOS/Linux.
PY_VER="3.12.15"
PY_TAG="20261009"
ENV_DIR="$ROOT/runtime"
PY_DIR="$ENV_DIR/python"
VENV_DIR="$ENV_DIR/venv"

fail() { echo "ERROR: $*" >&2; exit 1; }
banner() {
  echo "============================================================================"
  printf '%s\n' "$@"
  echo "============================================================================"
}

# Everything runs from main(): an update overwrites $ROOT/install.sh, and bash reads a running
# script from disk as it goes, so the whole of it has to be parsed before anything is copied.
main() {
  case "$(uname -s)-$(uname -m)" in
    Darwin-arm64)                PY_TRIPLE="aarch64-apple-darwin" ;;
    Darwin-x86_64)               PY_TRIPLE="x86_64-apple-darwin" ;;
    Linux-x86_64)                PY_TRIPLE="x86_64-unknown-linux-gnu" ;;
    Linux-aarch64 | Linux-arm64) PY_TRIPLE="aarch64-unknown-linux-gnu" ;;
    *) fail "unsupported system: $(uname -s) $(uname -m)" ;;
  esac
  PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/$PY_TAG/cpython-$PY_VER+$PY_TAG-$PY_TRIPLE-install_only.tar.gz"

  command -v curl >/dev/null || fail "curl is not installed"
  command -v tar >/dev/null || fail "tar is not installed"

  banner "Installing Internet stability monitor" "Target folder: $ROOT"
  if [ -f "$ROOT/app.py" ]; then
    echo "Existing installation found: updating it. config.json and data/ are kept."
  fi
  mkdir -p "$ROOT"

  TMP_DIR="$(mktemp -d)"
  trap 'rm -rf "$TMP_DIR"' EXIT

  banner "Downloading the project from GitHub"
  curl -fL --retry 3 -o "$TMP_DIR/project.tar.gz" "$REPO_URL" || fail "could not download $REPO_URL"
  mkdir "$TMP_DIR/project"
  tar -xzf "$TMP_DIR/project.tar.gz" -C "$TMP_DIR/project" --strip-components=1
  [ -f "$TMP_DIR/project/app.py" ] || fail "the archive has no app.py"
  # Copied over the old code: config.json, data/ and runtime/ are not in the repository, so they survive.
  cp -R "$TMP_DIR/project/." "$ROOT/"
  chmod +x "$ROOT/start.sh" "$ROOT/start.command" "$ROOT/install.sh"

  if [ -x "$PY_DIR/bin/python3" ] && "$PY_DIR/bin/python3" --version >/dev/null 2>&1; then
    echo "Python is already installed: $("$PY_DIR/bin/python3" --version)"
  else
    banner "Installing Python $PY_VER"
    rm -rf "$PY_DIR" "$VENV_DIR"
    mkdir -p "$ENV_DIR"
    curl -fL --retry 3 -o "$TMP_DIR/python.tar.gz" "$PY_URL" || fail "could not download $PY_URL"
    # The archive holds a single python/ folder.
    tar -xzf "$TMP_DIR/python.tar.gz" -C "$ENV_DIR"
    "$PY_DIR/bin/python3" --version || fail "Python failed to start"
  fi

  if [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "Creating virtual environment..."
    "$PY_DIR/bin/python3" -m venv "$VENV_DIR"
  fi

  banner "Installing requirements"
  "$VENV_DIR/bin/python" -m pip install --upgrade pip
  "$VENV_DIR/bin/python" -m pip install --upgrade -r "$ROOT/requirements.txt"

  if [ "$(uname -s)" = "Darwin" ]; then
    START="double-click $ROOT/start.command, or run $ROOT/start.sh"
  else
    START="run $ROOT/start.sh"
  fi
  banner "INSTALLATION SUCCESSFUL: $ROOT" "Start the monitor: $START" \
    "If it was already running, restart it to pick up the update." \
    "After that it checks GitHub for updates and installs them by itself."
}

main "$@"; exit
