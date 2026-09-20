#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

TEXT_MODEL="${TEXT_MODEL:-llama3.1:8b}"
VISION_MODEL="${VISION_MODEL:-moondream}"
OLLAMA_URL="http://localhost:11434"
OS="$(uname -s)"
PYTHON=""

if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  SUDO="sudo"
fi

info() { printf '\n\033[1;34m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33mWarning:\033[0m %s\n' "$1"; }
fail() { printf '\033[1;31mError:\033[0m %s\n' "$1"; exit 1; }

ollama_up() {
  curl -fs "$OLLAMA_URL/api/tags" >/dev/null 2>&1
}

load_brew() {
  if [ -x /opt/homebrew/bin/brew ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [ -x /usr/local/bin/brew ]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
}

install_macos() {
  load_brew
  if ! command -v brew >/dev/null 2>&1; then
    info "Installing Homebrew"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    load_brew
  fi
  command -v brew >/dev/null 2>&1 || fail "Homebrew is not available"
  info "Installing Python, ffmpeg, and Ollama"
  brew install python ffmpeg ollama
  PYTHON="$(brew --prefix)/bin/python3"
  if [ ! -x "$PYTHON" ]; then
    PYTHON="$(command -v python3 || true)"
  fi
}

linux_deps_present() {
  command -v python3 >/dev/null 2>&1 \
    && python3 -c 'import venv, ensurepip' >/dev/null 2>&1 \
    && command -v ffmpeg >/dev/null 2>&1 \
    && command -v curl >/dev/null 2>&1
}

install_linux() {
  if linux_deps_present; then
    info "Python, ffmpeg, and curl already installed"
  else
    info "Installing Python, ffmpeg, and curl"
    if command -v apt-get >/dev/null 2>&1; then
      $SUDO apt-get update
      $SUDO apt-get install -y python3 python3-venv python3-pip ffmpeg curl
    elif command -v dnf >/dev/null 2>&1; then
      $SUDO dnf install -y python3 python3-pip curl
      $SUDO dnf install -y ffmpeg || $SUDO dnf install -y ffmpeg-free
    elif command -v pacman >/dev/null 2>&1; then
      $SUDO pacman -Sy --needed --noconfirm python python-pip ffmpeg curl
    elif command -v zypper >/dev/null 2>&1; then
      $SUDO zypper --non-interactive install python3 python3-pip ffmpeg curl
    else
      fail "Unsupported package manager. Install python3 (3.10+) with venv, ffmpeg, and curl, then rerun"
    fi
  fi
  if command -v ollama >/dev/null 2>&1; then
    info "Ollama already installed"
  else
    info "Installing Ollama"
    curl -fsSL https://ollama.com/install.sh | sh
  fi
  PYTHON="$(command -v python3 || true)"
}

start_ollama() {
  if ollama_up; then
    echo "Ollama is already running"
    return
  fi
  if [ "$OS" = "Darwin" ]; then
    brew services start ollama
  elif command -v systemctl >/dev/null 2>&1 && systemctl cat ollama.service >/dev/null 2>&1; then
    $SUDO systemctl enable --now ollama
  else
    mkdir -p data/logs
    nohup ollama serve > data/logs/ollama.log 2>&1 &
  fi
  for _ in $(seq 1 30); do
    if ollama_up; then
      return
    fi
    sleep 1
  done
  fail "Ollama did not start. Try running: ollama serve"
}

[ -f app.py ] || fail "Run this script from the shorts-autopilot folder"

case "$OS" in
  Darwin) install_macos ;;
  Linux) install_linux ;;
  *) fail "Unsupported OS: $OS" ;;
esac

if [ -z "$PYTHON" ] || [ ! -x "$PYTHON" ]; then
  fail "python3 was not found after install"
fi
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || fail "Python 3.10 or newer is required (found $("$PYTHON" --version 2>&1))"
info "Using $("$PYTHON" --version)"

info "Starting Ollama"
start_ollama

info "Downloading AI models (this can take a while)"
ollama pull "$TEXT_MODEL"
ollama pull "$VISION_MODEL"

info "Setting up Python virtual environment"
if [ ! -x .venv/bin/python ]; then
  "$PYTHON" -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

info "Updating .gitignore"
touch .gitignore
for entry in data/ videos/ models/ tmp/ client_secret.json __pycache__/ '*.pyc' .venv/; do
  if ! grep -qxF "$entry" .gitignore; then
    echo "$entry" >> .gitignore
  fi
done

chmod +x run.sh install-local.sh 2>/dev/null || true

info "Checking setup"
if [ -f client_secret.json ]; then
  echo "client_secret.json found"
else
  warn "client_secret.json is missing. Download it from Google Cloud and place it next to app.py"
fi
echo "For the free local video engine, run ./install-local.sh next"
echo "Models can also be added later from the Settings dropdowns"

info "Install complete"
echo "Start the app any time with: ./run.sh"
answer="n"
read -r -p "Start it now? [Y/n] " answer || answer="n"
case "${answer:-Y}" in
  [Yy]*) exec ./run.sh ;;
esac
