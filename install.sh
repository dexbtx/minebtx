#!/usr/bin/env bash
# DEXBTX miner — one-line installer.
#
# Usage:
#   curl -fsSL https://minebtx.com/install.sh | bash
#   curl -fsSL https://minebtx.com/install.sh | bash -s -- --address btx1z...
#
# What this script does:
#   1. Detect OS + GPU (NVIDIA via nvidia-smi; otherwise CPU-only path)
#   2. Install Python 3.10+ if missing
#   3. Install dexbtx-miner via pip
#   4. Download the patched btx-gbt-solve binary, verify SHA256 against
#      the value pinned in pyproject.toml
#   5. Prompt for the user's btx1z... payout address (or accept --address)
#   6. Save config to ~/.dexbtx-miner/config.yaml
#   7. Print the launch command (and an optional systemd unit)
#
# This script is idempotent — re-running upgrades to the latest stable
# version. Existing config is preserved.

set -euo pipefail

# ─── Configurables ──────────────────────────────────────────────────────────
# Pin the prebuilds release tag. install.sh always pulls this version.
# Bump in lockstep with experiments/vast/prebuilds and pyproject.toml.
PREBUILDS_TAG="${PREBUILDS_TAG:-btx-prebuilds-v0.32.2}"
EXPECTED_SHA256="${EXPECTED_SHA256:-86fd2f6de99cf735129fa1cb0f71078901bf22a34d21a682c547ab5eccd47a81}"
PREBUILDS_BASE="${PREBUILDS_BASE:-https://github.com/dexbtx/minebtx/releases/download/${PREBUILDS_TAG}}"
SOLVER_URL="${PREBUILDS_BASE}/btx-gbt-solve"

# Default pool — override with --pool flag or DEXBTX_POOL env var.
DEFAULT_POOL="${DEXBTX_POOL:-minebtx.com:3333}"

# Install paths.
INSTALL_DIR="${HOME}/.dexbtx-miner"
SOLVER_PATH="${INSTALL_DIR}/bin/btx-gbt-solve"
CONFIG_PATH="${INSTALL_DIR}/config.yaml"

# ─── Parse CLI ──────────────────────────────────────────────────────────────
ADDRESS=""
WORKER=""
POOL="${DEFAULT_POOL}"
ASSUME_YES=0
SKIP_PROMPT=0
LOCAL_SOLVER=""   # if set: copy from this local path instead of downloading
SKIP_PIP=0        # if 1: don't pip install dexbtx-miner (useful when running from a source checkout)

while [[ $# -gt 0 ]]; do
    case "$1" in
        --address) ADDRESS="$2"; shift 2 ;;
        --worker)  WORKER="$2";  shift 2 ;;
        --pool)    POOL="$2";    shift 2 ;;
        --yes|-y)  ASSUME_YES=1; SKIP_PROMPT=1; shift ;;
        --skip-prompt) SKIP_PROMPT=1; shift ;;
        --local-solver) LOCAL_SOLVER="$2"; shift 2 ;;
        --skip-pip)    SKIP_PIP=1; shift ;;
        --help|-h)
            sed -n '2,17p' "$0"
            exit 0
            ;;
        *) echo "unknown arg: $1"; exit 1 ;;
    esac
done

# ─── Helpers ────────────────────────────────────────────────────────────────
log()  { echo -e "\033[1;34m[install]\033[0m $*"; }
warn() { echo -e "\033[1;33m[warn]\033[0m $*" >&2; }
err()  { echo -e "\033[1;31m[error]\033[0m $*" >&2; exit 1; }

need() {
    command -v "$1" >/dev/null 2>&1 || err "missing required tool: $1"
}

confirm() {
    [[ "$ASSUME_YES" -eq 1 ]] && return 0
    read -rp "$1 [y/N] " ans
    [[ "$ans" =~ ^[Yy]$ ]]
}

# ─── Detection ──────────────────────────────────────────────────────────────
log "DEXBTX miner installer — release ${PREBUILDS_TAG}"

OS="$(uname -s)"
case "$OS" in
    Linux)  : ;;
    Darwin) warn "macOS detected. Solver supports Metal backend (M-series only); NVIDIA path will not run." ;;
    *)      err "unsupported OS: $OS" ;;
esac

# GPU detection
HAS_NVIDIA=0
GPU_NAME=""
if command -v nvidia-smi >/dev/null 2>&1; then
    if nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 | grep -q "."; then
        HAS_NVIDIA=1
        GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
        log "detected NVIDIA GPU: ${GPU_NAME}"
    fi
fi
if [[ "$HAS_NVIDIA" -eq 0 ]]; then
    warn "no NVIDIA GPU detected — solver will run on CPU only (much slower)"
fi

# Python
need curl
need sha256sum

PYTHON=""
for cand in python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
        PYTHON="$cand"
        if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
            break
        fi
        PYTHON=""
    fi
done

if [[ -z "$PYTHON" ]]; then
    log "installing python3.11 via apt..."
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update -qq
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3.11 python3.11-venv python3-pip
        PYTHON=python3.11
    else
        err "no python3.10+ found and apt-get not available — install Python 3.10+ manually then re-run"
    fi
fi
log "using Python: $($PYTHON --version 2>&1)"

# ─── Install pip + runtime deps + dexbtx-miner ──────────────────────────────
# Many vast.ai CUDA images ship without pip — install it via apt if missing.
if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
    log "python pip not present; installing via apt..."
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update -qq
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-pip
    else
        err "python pip missing and apt-get not available; install pip manually then re-run"
    fi
fi

# Runtime deps (pyyaml for --config). Install regardless of --skip-pip
# because --skip-pip only skips the dexbtx-miner package itself (useful for
# source-tree dev), not its transitive deps.
log "installing runtime deps (pyyaml)..."
"$PYTHON" -m pip install --user --quiet --upgrade pyyaml

if [[ "$SKIP_PIP" -eq 1 ]]; then
    log "skipping dexbtx-miner pip install (--skip-pip); assuming source tree is on PYTHONPATH"
else
    # Install the Python package directly from the GitHub source tarball
    # for the v0.3 release tag. We do NOT publish to PyPI — fetching from
    # GitHub keeps the install pinned to a specific release commit and
    # avoids a third-party package surface. Override DEXBTX_MINER_PKG_URL
    # to install from a fork or a different ref.
    DEXBTX_MINER_PKG_URL="${DEXBTX_MINER_PKG_URL:-https://github.com/dexbtx/minebtx/archive/refs/tags/v0.3.4.tar.gz}"
    log "installing dexbtx-miner from ${DEXBTX_MINER_PKG_URL} (pip --user)..."
    "$PYTHON" -m pip install --user --upgrade "$DEXBTX_MINER_PKG_URL"

    # Make sure ~/.local/bin is on PATH for the next session
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) : ;;
        *) warn "add to your shell rc: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
    esac
fi

# ─── Fetch + verify solver ──────────────────────────────────────────────────
mkdir -p "${INSTALL_DIR}/bin"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

if [[ -n "$LOCAL_SOLVER" ]]; then
    log "using local solver at ${LOCAL_SOLVER} (skipping download)"
    cp "$LOCAL_SOLVER" "$TMP"
else
    log "downloading patched btx-gbt-solve from ${SOLVER_URL}..."
    curl -fsSL "$SOLVER_URL" -o "$TMP"
fi

ACTUAL_SHA="$(sha256sum "$TMP" | awk '{print $1}')"
if [[ "$ACTUAL_SHA" != "$EXPECTED_SHA256" ]]; then
    err "solver SHA256 mismatch — expected $EXPECTED_SHA256, got $ACTUAL_SHA. Aborting (refusing to install untrusted binary)."
fi
log "solver SHA256 verified ($EXPECTED_SHA256)"

install -m 0755 "$TMP" "$SOLVER_PATH"
log "solver installed → $SOLVER_PATH"

# Smoke-test the binary. NOTE: btx-gbt-solve --help exits with code 1 (the
# upstream convention), so with `set -o pipefail` enabled we can't use
# `if ! cmd | grep` directly — capture the output first, grep separately.
HELP_OUT="$("$SOLVER_PATH" --help 2>&1 || true)"
if echo "$HELP_OUT" | grep -q "share-target"; then
    log "smoke-test: --share-target flag present ✓"
else
    err "installed solver lacks --share-target flag (the patch this release needs). Aborting."
fi

# ─── Config ─────────────────────────────────────────────────────────────────
if [[ -z "$ADDRESS" && "$SKIP_PROMPT" -eq 0 ]]; then
    echo
    echo "Enter your BTX payout address (format: btx1z...):"
    read -rp "  address: " ADDRESS
fi

if [[ -n "$ADDRESS" ]]; then
    if [[ ! "$ADDRESS" =~ ^btx1z[0-9a-zA-Z]{50,}$ ]]; then
        warn "address does not match expected btx1z... format — proceeding anyway, but double-check"
    fi
fi

if [[ -z "$WORKER" ]]; then
    WORKER="$(hostname -s 2>/dev/null || echo default)"
fi

# Write config (preserve existing fields if file exists)
if [[ -f "$CONFIG_PATH" ]]; then
    log "config exists at $CONFIG_PATH — preserving (override with --yes to regenerate)"
    if [[ "$ASSUME_YES" -eq 1 ]]; then
        cp "$CONFIG_PATH" "${CONFIG_PATH}.bak.$(date +%s)"
        log "backed up old config"
    fi
fi

if [[ ! -f "$CONFIG_PATH" || "$ASSUME_YES" -eq 1 ]]; then
    # Per-GPU tuning suggestions. The default (batch=128) is good for
    # modern Ampere+/Ada/Blackwell. Pascal-era cards (sm_61) benefit from
    # batch=32. Honor the user's hardware:
    GPU_BATCH=128
    GPU_PREFETCH=8
    GPU_WORKERS=8
    if [[ "$GPU_NAME" == *"GTX 10"* || "$GPU_NAME" == *"GTX 9"* ]]; then
        GPU_BATCH=32  # Pascal+older sweet spot per memory project_btx_solver_tuning_findings
        GPU_PREFETCH=4
        GPU_WORKERS=4
    fi
    cat > "$CONFIG_PATH" <<YAML
# DEXBTX miner config — generated by install.sh
# Production-canonical solver tuning: env vars match upstream BTX matmul
# source names verified via grep getenv. Wrong names silently no-op
# (we shipped BTX_MATMUL_PREFETCH for a while; it doesn't exist).
pool_host: "${POOL%:*}"
pool_port: ${POOL##*:}
pool_tls: false

payout_address: "${ADDRESS}"
worker_name: "${WORKER}"

gbt_solve_path: "${SOLVER_PATH}"

# Solver tuning (per-GPU profile chosen from detected hardware: ${GPU_NAME:-CPU only})
solver_backend: "cuda"
solver_threads: 4
solver_batch_size: ${GPU_BATCH}        # BTX_MATMUL_SOLVE_BATCH_SIZE
solver_prefetch_depth: ${GPU_PREFETCH} # BTX_MATMUL_PREPARE_PREFETCH_DEPTH
solver_prepare_workers: ${GPU_WORKERS} # BTX_MATMUL_PREPARE_WORKERS
solver_pipeline_async: 1               # BTX_MATMUL_PIPELINE_ASYNC (overlap prep+kernel)
gpu_inputs: 0                          # BTX_MATMUL_GPU_INPUTS (CPU-gen inputs; the "GPU saturation breakthrough" fix)

nonces_per_slice: 2000000
reconnect_initial_s: 1.0
reconnect_max_s: 60.0

log_level: "INFO"
YAML
    log "config written → $CONFIG_PATH (profile: batch=${GPU_BATCH} prefetch=${GPU_PREFETCH} workers=${GPU_WORKERS})"
fi

# ─── Hard GPU acceleration smoke test ───────────────────────────────────────
# Runs the solver with --backend cuda against a deterministic input. If the
# CUDA backend doesn't engage (driver missing, sm not supported, CUDA OOM,
# etc.) the solver falls back to CPU which is ~100× slower — the alpha
# cohort would never know. Fail HARD so the operator notices at install time.
if [[ "$HAS_NVIDIA" -eq 1 ]]; then
    log "running GPU acceleration smoke test (5-10 seconds)..."
    SMOKE_OUT="$("$SOLVER_PATH" \
        --version 536870912 \
        --prev-hash 0ab38fdff2ef667dcddac7f50c3696080c26697615f7b6b9af5c3a1ba0a5fb7e \
        --merkle-root d906f02ed11d8936770423263b56c5ffe1ea1b15c8a2867afb161adb6fd76eb7 \
        --time 1779672814 --bits 0x1d17c609 \
        --share-target 00ffffff00000000000000000000000000000000000000000000000000000000 \
        --seed-a 8460daf3ff446cc55a7115de88ee24c8a2bf182eedde43abb9cf4cc94cc209bf \
        --seed-b 7f2e377616feb92d2e9857cab390595b7d6b8d24373a2da394f8d97197b5f437 \
        --block-height 110806 --nonce-start 1 \
        --max-tries 200000 --max-seconds 30 \
        --backend cuda --solver-threads 4 --batch-size 32 2>&1 || true)"
    SMOKE_LAST_LINE="$(echo "$SMOKE_OUT" | grep -E '^\{.*\}$' | tail -1)"
    if [[ -z "$SMOKE_LAST_LINE" ]]; then
        echo "$SMOKE_OUT" | tail -10 >&2
        err "GPU smoke test: solver produced no JSON output. CUDA backend likely failed to initialize. Aborting."
    fi
    if echo "$SMOKE_LAST_LINE" | grep -q '"found":true'; then
        ELAPSED="$(echo "$SMOKE_LAST_LINE" | sed -E 's/.*"elapsed_s":([0-9.e+-]+).*/\1/')"
        log "GPU smoke test: PASS (found a share in ${ELAPSED}s)"
    else
        echo "$SMOKE_LAST_LINE" >&2
        warn "GPU smoke test: solver ran but didn't find a share — could be hard luck OR CPU fallback. Continuing, but watch first share latency."
    fi
fi

# ─── Summary ────────────────────────────────────────────────────────────────
echo
log "✓ DEXBTX miner installed."
echo
echo "  Pool:     ${POOL}"
echo "  Address:  ${ADDRESS:-<edit ${CONFIG_PATH} and set payout_address>}"
echo "  Worker:   ${WORKER}"
echo "  GPU:      ${GPU_NAME:-CPU only}"
echo
echo "Launch the miner:"
echo "  dexbtx-miner --config ${CONFIG_PATH}"
echo
echo "Or, for a long-running daemon (recommended):"
echo "  tmux new -d -s dexbtx 'dexbtx-miner --config ${CONFIG_PATH} 2>&1 | tee -a ${INSTALL_DIR}/miner.log'"
echo "  tmux attach -t dexbtx"
echo
echo "Stats + payouts via Telegram: @btxdexbot   /stats /mybalance /help"
echo
echo "Tune for your specific GPU (the defaults are a starting point — every"
echo "card has a different sweet spot):"
echo "  dexbtx-miner benchmark                  # 2-min sweep across common batch sizes"
echo "  dexbtx-miner benchmark --write-config   # write the winning config"
echo "See TUNING.md for the env-var reference + per-GPU rough guidelines."
echo
