#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

info() { printf '\n\033[1;34m==>\033[0m %s\n' "$1"; }
fail() { printf '\033[1;31mError:\033[0m %s\n' "$1"; exit 1; }

[ -x .venv/bin/python ] || fail "Run ./install.sh first"

if [ -n "${PIP_CACHE_DIR:-}" ]; then
  mkdir -p "$PIP_CACHE_DIR"
  info "Using pip cache at $PIP_CACHE_DIR"
fi

info "Installing local image generation packages (a few GB)"
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-local.txt

if [ "$(uname -s)" = "Linux" ] && ! command -v espeak-ng >/dev/null 2>&1; then
  info "Installing espeak-ng for text to speech"
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get install -y espeak-ng
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y espeak-ng
  elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -Sy --needed --noconfirm espeak-ng
  elif command -v zypper >/dev/null 2>&1; then
    sudo zypper --non-interactive install espeak-ng
  fi
fi

info "Installing Piper for natural sounding narration"
.venv/bin/python -m pip install piper-tts || echo "Piper install failed, narration will fall back to the system voice"

VOICE_DIR="${VOICE_DIR:-}"
if [ -z "$VOICE_DIR" ]; then
  VOICE_DIR="$(.venv/bin/python - <<'PY'
import json
from pathlib import Path
base = Path(__file__).resolve().parent if "__file__" in dir() else Path.cwd()
try:
    g = json.loads((Path("data") / "settings.json").read_text())
except Exception:
    g = {}
models = str(g.get("models_dir") or "").strip()
storage = str(g.get("storage_dir") or "videos").strip()
root = Path(models) if models else Path(storage) / "models"
print(root / "voices")
PY
)"
fi
mkdir -p "$VOICE_DIR"
VOICE_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium/en_US-lessac-medium"
if [ ! -f "$VOICE_DIR/en_US-lessac-medium.onnx" ]; then
  info "Downloading the Piper voice into $VOICE_DIR"
  curl -fL --progress-bar -o "$VOICE_DIR/en_US-lessac-medium.onnx" "$VOICE_BASE.onnx" \
    && curl -fL --progress-bar -o "$VOICE_DIR/en_US-lessac-medium.onnx.json" "$VOICE_BASE.onnx.json" \
    || echo "Voice download failed, narration will fall back to the system voice"
else
  info "Piper voice already downloaded"
fi

info "Checking torch"
.venv/bin/python - <<'EOF'
import torch
if torch.backends.mps.is_available():
    device = "mps (Apple GPU)"
elif torch.cuda.is_available():
    device = "cuda"
else:
    device = "cpu (slow)"
print(f"torch {torch.__version__} will run on {device}")
EOF

info "Done"
echo "In Settings set Video engine to local, set Models folder and Temp folder, then Save"
echo "The first run downloads about 7 GB into your Models folder"
echo "Narration uses Piper automatically when a voice is present in the voices folder"
