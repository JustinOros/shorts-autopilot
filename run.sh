#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

OS="$(uname -s)"
URL="http://localhost:8000"

if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  SUDO="sudo"
fi

ollama_up() {
  curl -fs http://localhost:11434/api/tags >/dev/null 2>&1
}

if [ "$OS" = "Darwin" ]; then
  if [ -x /opt/homebrew/bin/brew ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [ -x /usr/local/bin/brew ]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
fi

if [ ! -x .venv/bin/python ]; then
  echo "Run ./install.sh first"
  exit 1
fi

if ! ollama_up; then
  echo "Starting Ollama"
  if [ "$OS" = "Darwin" ]; then
    brew services start ollama >/dev/null 2>&1 || true
  elif command -v systemctl >/dev/null 2>&1 && systemctl cat ollama.service >/dev/null 2>&1; then
    $SUDO systemctl start ollama || true
  else
    mkdir -p data/logs
    nohup ollama serve > data/logs/ollama.log 2>&1 &
  fi
  for _ in $(seq 1 15); do
    if ollama_up; then
      break
    fi
    sleep 1
  done
  if ! ollama_up; then
    echo "Warning: Ollama is not responding, script generation will fail until it is running"
  fi
fi

open_browser() {
  sleep 3
  if [ "$OS" = "Darwin" ]; then
    open "$URL"
  elif { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; } && command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1
  fi
}

echo "Shorts Autopilot: $URL"
open_browser &
exec .venv/bin/python app.py
