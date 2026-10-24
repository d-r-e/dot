#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if [ -f ../.env ]; then
  set -a
  # shellcheck source=/dev/null
  source ../.env
  set +a
fi

PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
VENV=".venv"
MODEL="${MODEL:-models/Nemotron-3.5-Bolt-E4B-Q4_K_M.gguf}"
DRAFTER="${DRAFTER:-models/Nemotron-3.5-Bolt-E4B-DFlash-BF16.gguf}"

fetch_models() {
  command -v hf >/dev/null 2>&1 || {
    echo "error: the 'hf' CLI is required for --fetch" >&2
    exit 1
  }
  mkdir -p models
  echo "==> downloading llama.cpp weights (6.18 GB + 0.74 GB)"
  hf download nvidia/NVIDIA-Nemotron-3.5-Bolt-E4B-4GB \
    Nemotron-3.5-Bolt-E4B-Q4_K_M.gguf --local-dir models
  hf download nvidia/NVIDIA-Nemotron-3.5-Bolt-E4B-4GB-DFlash \
    Nemotron-3.5-Bolt-E4B-DFlash-BF16.gguf --local-dir models
  echo "==> warming the MLX speech models (~1.1 GB)"
  hf download mlx-community/silero-vad >/dev/null
  hf download mlx-community/nemotron-3.5-asr-streaming-0.6b-8bit >/dev/null
  hf download mlx-community/Kokoro-82M-8bit >/dev/null
  echo "==> done"
}

ensure_venv() {
  command -v uv >/dev/null 2>&1 || {
    echo "error: uv is required (https://docs.astral.sh/uv/)" >&2
    exit 1
  }
  if [ -x "$VENV/bin/python" ]; then
    have="$("$VENV/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    if [ "$have" != "$PYTHON_VERSION" ]; then
      echo "error: $VENV uses Python $have, need $PYTHON_VERSION" >&2
      echo "       remove $VENV and rerun (Kokoro/misaki requires Python < 3.13)" >&2
      exit 1
    fi
  else
    echo "==> creating $VENV (python $PYTHON_VERSION)"
    uv venv --python "$PYTHON_VERSION" "$VENV"
  fi
  if [ ! -f "$VENV/.deps-ok" ] || [ requirements.txt -nt "$VENV/.deps-ok" ]; then
    echo "==> installing python dependencies"
    uv pip install --python "$VENV/bin/python" -r requirements.txt
    touch "$VENV/.deps-ok"
  fi
  # Upgrade existing headless-only environments as well as new installations.
  # Reinstall after dependency updates so Chromium matches the pinned Playwright.
  if [ ! -f "$VENV/.chromium-full-ok" ] || [ "$VENV/.deps-ok" -nt "$VENV/.chromium-full-ok" ]; then
    "$VENV/bin/python" -m playwright install chromium
    touch "$VENV/.chromium-full-ok"
  fi
}

case "${1:-}" in
  --fetch)
    fetch_models
    exit 0
    ;;
  --help|-h)
    echo "usage: ./run.sh [--fetch]"
    echo "  --fetch   download the GGUF and MLX speech models, then exit"
    exit 0
    ;;
esac

ensure_venv

if [ ! -f "$MODEL" ]; then
  if [ "$MODEL" = "models/Nemotron-3.5-Bolt-E4B-Q4_K_M.gguf" ]; then
    echo "==> model missing, fetching"
    fetch_models
  else
    echo "error: model not found: $MODEL" >&2
    echo "       custom MODEL paths must be downloaded manually" >&2
    exit 1
  fi
fi

HOST="${DOT_HOST:-127.0.0.1}"
PORT="${DOT_PORT:-8765}"
SCHEME="http"
SSL_KEY=""
SSL_CERT=""

lan_ip() {
  ifconfig 2>/dev/null | awk '/inet /{print $2}' | grep -v '^127\.' | head -n 1
}

if [ "${DOT_TLS:-0}" = "1" ]; then
  SCHEME="https"
  TLS_DIR=".tls"
  SSL_KEY="$TLS_DIR/privkey.pem"
  SSL_CERT="$TLS_DIR/fullchain.pem"
  if [ ! -f "$SSL_KEY" ] || [ ! -f "$SSL_CERT" ]; then
    # Browsers only expose the microphone on a secure origin, so a LAN client
    # needs https. The certificate is self-signed and local to this machine.
    command -v openssl >/dev/null 2>&1 || {
      echo "error: DOT_TLS=1 needs openssl to create a local certificate" >&2
      exit 1
    }
    mkdir -p "$TLS_DIR"
    name="$(scutil --get LocalHostName 2>/dev/null || hostname -s)"
    san="DNS:localhost,DNS:${name}.local,IP:127.0.0.1,IP:::1"
    for ip in $(ifconfig 2>/dev/null | awk '/inet /{print $2}' | grep -v '^127\.'); do
      san="$san,IP:$ip"
    done
    echo "==> creating a self-signed certificate for ${san}"
    openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 825 \
      -keyout "$SSL_KEY" -out "$SSL_CERT" -subj "/CN=dot" \
      -addext "subjectAltName=$san" >/dev/null 2>&1
    chmod 600 "$SSL_KEY"
  fi
fi

display_host="$HOST"
case "$HOST" in
  0.0.0.0|"::") display_host="$(lan_ip)"
     [ -n "$display_host" ] || display_host="127.0.0.1" ;;
esac

echo "=================================================="
echo " dot        ${SCHEME}://${display_host}:${PORT}"
echo " bind       ${HOST}:${PORT}"
echo " llama.cpp  http://${LLAMA_HOST:-127.0.0.1}:${LLAMA_PORT:-1234}"
echo " model      ${MODEL}"
echo "=================================================="

set -- --host "$HOST" --port "$PORT" --log-level info --timeout-graceful-shutdown 5
if [ -n "$SSL_KEY" ]; then
  set -- "$@" --ssl-keyfile "$SSL_KEY" --ssl-certfile "$SSL_CERT"
fi
exec "$VENV/bin/python" -m uvicorn app:app "$@"
