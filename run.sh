#!/usr/bin/env bash
# approval-flow demo runner.
#
#   ./run.sh            full demo on REAL Temporal (dev server, auto-downloaded)
#   ./run.sh --local    full demo on the local durable runner (no Temporal)
#   ./run.sh --serve    leave the stack up for manual play (Temporal)
#   ./run.sh --serve-local  leave the stack up (local runner mode)
#
# Temporal mode needs the `temporal` CLI (dev server). If it is not on
# PATH, this script downloads the official release binary into .tools/
# (gitignored). If the download or the server fails, it says so plainly
# and runs the demo in --local mode instead of faking a Temporal run.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

MODE="temporal"
SERVE=0
for arg in "$@"; do
  case "$arg" in
    --local) MODE="local" ;;
    --serve) SERVE=1 ;;
    --serve-local) MODE="local"; SERVE=1 ;;
    *) echo "unknown arg: $arg (want --local, --serve, --serve-local)"; exit 2 ;;
  esac
done

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export APPROVAL_FLOW_DB="$ROOT/data/state.db"
export MOCK_SLACK_URL="http://127.0.0.1:18765"
export TEMPORAL_ADDRESS="localhost:7233"

# --- venv + deps (repo-local, never touches system Python) -------------------
if [ ! -x .venv/bin/python ]; then
  echo "[run] creating venv..."
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt -r requirements-dev.txt
echo "[run] deps OK: $(.venv/bin/python -c 'import temporalio; print("temporalio", temporalio.__version__)')"

# --- Temporal CLI (dev server) ----------------------------------------------
TEMPORAL_BIN=""
if [ "$MODE" = "temporal" ]; then
  if command -v temporal >/dev/null 2>&1; then
    TEMPORAL_BIN="$(command -v temporal)"
  elif [ -x .tools/temporal ]; then
    TEMPORAL_BIN="$ROOT/.tools/temporal"
  else
    ARCH="$(uname -m)"
    case "$ARCH" in
      x86_64) TARCH="amd64" ;;
      aarch64|arm64) TARCH="arm64" ;;
      *) echo "[run] unsupported arch for Temporal CLI download: $ARCH"; TARCH="" ;;
    esac
    if [ -n "$TARCH" ]; then
      VER="1.10.1"
      URL="https://github.com/temporalio/cli/releases/download/v${VER}/temporal_cli_${VER}_linux_${TARCH}.tar.gz"
      echo "[run] downloading Temporal CLI ${VER} (dev server) ..."
      mkdir -p .tools
      if curl -sL --max-time 180 -o .tools/cli.tar.gz "$URL" \
          && tar --no-same-owner -xzf .tools/cli.tar.gz -C .tools temporal \
          && chmod +x .tools/temporal; then
        rm -f .tools/cli.tar.gz
        TEMPORAL_BIN="$ROOT/.tools/temporal"
        echo "[run] Temporal CLI ready: $("$TEMPORAL_BIN" --version | head -1)"
      else
        echo "[run] WARNING: could not download Temporal CLI — falling back to local mode."
        MODE="local"
      fi
    else
      MODE="local"
    fi
  fi
fi

# --- fresh state ---------------------------------------------------------------
rm -f data/state.db data/state.db-shm data/state.db-wal
rm -f data/temporal.db data/temporal.db-shm data/temporal.db-wal

SERVER_PID=""
cleanup() {
  if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

wait_port() {  # wait_port <port> <seconds> <name>
  .venv/bin/python - "$1" "$2" "$3" <<'PYEOF'
import socket, sys, time
port, timeout, name = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
deadline = time.monotonic() + timeout
while time.monotonic() < deadline:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            sys.exit(0)
    except OSError:
        time.sleep(0.5)
print(f"{name} did not listen on port {port} within {timeout}s", file=sys.stderr)
sys.exit(1)
PYEOF
}

if [ "$MODE" = "temporal" ]; then
  echo "[run] starting Temporal dev server (SQLite, data/temporal.db)..."
  "$TEMPORAL_BIN" server start-dev --db-filename data/temporal.db \
    --port 7233 --ui-port 8233 --log-level error \
    > data/temporal-server.log 2>&1 &
  SERVER_PID=$!
  if ! wait_port 7233 60 "Temporal dev server"; then
    echo "[run] WARNING: Temporal dev server failed to start (see data/temporal-server.log) — falling back to local mode."
    cleanup; SERVER_PID=""; trap - EXIT
    MODE="local"
  else
    # The port opens before the `default` namespace is registered; wait
    # for the namespace or the first workflow start fails with
    # "Namespace default is not found".
    echo "[run] waiting for the default namespace..."
    for _ in $(seq 1 60); do
      if "$TEMPORAL_BIN" operator namespace describe default \
          --address localhost:7233 >/dev/null 2>&1; then
        break
      fi
      sleep 1
    done
    echo "[run] Temporal dev server up: localhost:7233 (UI: http://127.0.0.1:8233)"
  fi
fi

if [ "$SERVE" = 1 ]; then
  echo "[run] starting mock Slack + worker — leaving the stack up."
  echo "[run]   mock Slack dashboard: http://127.0.0.1:18765/"
  [ "$MODE" = "temporal" ] && echo "[run]   Temporal UI:          http://127.0.0.1:8233/"
  echo "[run] press Ctrl-C to stop."
  .venv/bin/python -m approval_flow.seed --db "$APPROVAL_FLOW_DB" \
    --invoices-dir "$ROOT/data/invoices"
  .venv/bin/python -m approval_flow.mock_slack --mode "$MODE" --port 18765 \
    --db "$APPROVAL_FLOW_DB" &
  SLACK_PID=$!
  trap 'kill $SLACK_PID $WORKER_PID 2>/dev/null; cleanup' EXIT
  if [ "$MODE" = "temporal" ]; then
    .venv/bin/python -m approval_flow.worker &
    WORKER_PID=$!
    echo "[run] worker pid $WORKER_PID — start a workflow with:"
    echo "[run]   .venv/bin/python -c \"...\"  (see README 'Try it yourself')"
  else
    WORKER_PID=""
    echo "[run] local mode — run an invoice with:"
    echo "[run]   .venv/bin/python -m approval_flow.local_runner --invoice INV-2026-1042"
  fi
  wait
  exit 0
fi

echo "[run] running full demo ($MODE mode) — log: docs/demo-run.log"
set +e
.venv/bin/python demo.py --mode "$MODE" 2>&1 | tee docs/demo-run.log
STATUS=${PIPESTATUS[0]}
set -e
if [ "$STATUS" -ne 0 ]; then
  echo "[run] DEMO FAILED (exit $STATUS) — see docs/demo-run.log"
  exit "$STATUS"
fi
echo "[run] DEMO PASSED ($MODE mode)"
echo "[run]   approval card: docs/demo-approval-card.png"
echo "[run]   final state:   docs/demo-final.png"
echo "[run]   summary:       docs/demo-summary.json"
