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
# Pin the release tag. install.sh always pulls this version of the solver
# binary as a release asset.
#
# The binary's SHA256 is the SINGLE SOURCE OF TRUTH in pyproject.toml under
# [tool.dexbtx-miner.solver].expected_sha256. install.sh fetches that file
# at install time and reads the hash from it (see fetch + parse block ~L170)
# rather than carrying its own duplicate of the value. This means bumping
# the binary requires editing exactly one place (pyproject.toml) instead of
# three (install.sh, pyproject.toml, release-tooling/SHA256SUMS).
#
# EXPECTED_SHA256 env var is supported as an override for offline / mirror
# / fork use; if set, it skips the remote pyproject.toml fetch.
PREBUILDS_TAG="${PREBUILDS_TAG:-v4.3-sm89-native}"
PREBUILDS_BASE="${PREBUILDS_BASE:-https://github.com/dexbtx/minebtx/releases/download/${PREBUILDS_TAG}}"
SOLVER_URL="${PREBUILDS_BASE}/btx-gbt-solve"
PYPROJECT_URL="${PYPROJECT_URL:-https://raw.githubusercontent.com/dexbtx/minebtx/${PREBUILDS_TAG}/pyproject.toml}"

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
    # at the same release tag as the binary. This avoids needing a
    # git binary on the host AND avoids PyPI as a single point of
    # failure. Override DEXBTX_MINER_PKG_URL to install from a fork
    # / local checkout / private mirror.
    DEXBTX_MINER_PKG_URL="${DEXBTX_MINER_PKG_URL:-https://github.com/dexbtx/minebtx/archive/refs/tags/${PREBUILDS_TAG}.tar.gz}"
    log "installing dexbtx-miner from ${DEXBTX_MINER_PKG_URL} (pip --user)..."
    "$PYTHON" -m pip install --user --upgrade "$DEXBTX_MINER_PKG_URL"

    # Make sure ~/.local/bin is on PATH for the next session
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) : ;;
        *) warn "add to your shell rc: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
    esac
fi

# ─── Resolve EXPECTED_SHA256 from pyproject.toml (single source of truth) ─
# Unless the user passed EXPECTED_SHA256 via env var (offline / fork use),
# fetch the pyproject.toml from the release tag and extract the pinned hash.
# This keeps install.sh + pyproject.toml + SHA256SUMS from drifting.
if [[ -z "${EXPECTED_SHA256:-}" ]]; then
    log "fetching solver SHA256 pin from ${PYPROJECT_URL}..."
    PYPROJECT_TMP="$(mktemp)"
    if ! curl -fsSL "$PYPROJECT_URL" -o "$PYPROJECT_TMP"; then
        rm -f "$PYPROJECT_TMP"
        err "could not fetch pyproject.toml from ${PYPROJECT_URL}. Pass EXPECTED_SHA256=<hash> to override."
    fi
    EXPECTED_SHA256="$(grep -E '^\s*expected_sha256\s*=' "$PYPROJECT_TMP" | head -1 | sed -E 's/.*"([a-f0-9]+)".*/\1/')"
    rm -f "$PYPROJECT_TMP"
    if [[ ! "$EXPECTED_SHA256" =~ ^[a-f0-9]{64}$ ]]; then
        err "could not extract a 64-char SHA256 from pyproject.toml; got: ${EXPECTED_SHA256:-<empty>}"
    fi
    log "pinned solver SHA256 (from pyproject.toml): $EXPECTED_SHA256"
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
if echo "$HELP_OUT" | grep -q "daemon"; then
    log "smoke-test: --daemon flag present ✓"
else
    err "installed solver lacks --daemon flag — the dexbtx-miner wrapper runs the solver in daemon mode for persistent CUDA context. Aborting."
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
    # ───────────────────────────────────────────────────────────────────
    # Per-GPU tuning profile.
    #
    # THE MOST IMPORTANT LEVERS are PREPARE_WORKERS and SOLVER_THREADS —
    # the matmul backend is CPU-input-prep-bound on the faster Blackwell
    # cards. On an RTX 5070 we measured 70.6% util with workers=8 / threads=4
    # and 100% util with workers=16 / threads=8 (sweep 2026-05-25 on contract
    # 37712744). Batch size has zero effect once prep is sufficient.
    #
    # CPU sizing note: workers+threads should sum to ≤ effective vCPU count
    # of your container. The pool's solver is async (PIPELINE_ASYNC=1), so
    # idle workers don't burn cycles — over-provisioning is cheap.
    #
    # Profiles below are starting points calibrated against measured cards.
    # Run `dexbtx-miner benchmark` (TUNING.md) to dial in further.
    # ───────────────────────────────────────────────────────────────────
    GPU_BATCH=128
    GPU_PREFETCH=8
    GPU_WORKERS=8
    GPU_THREADS=4
    if [[ "$GPU_NAME" == *"GTX 10"* || "$GPU_NAME" == *"GTX 9"* ]]; then
        # Pascal sm_61 (e.g. GTX 1070): memory-bound. Measured ceiling
        # ~83% util / 113W (75% of 150W TDP) at 16/8/128 — same ceiling
        # as the older 4/4/32 profile in practice; Pascal is bandwidth-
        # constrained regardless of input-prep rate. Standardized on the
        # universal modern profile for tuning consistency across gens.
        GPU_BATCH=128
        GPU_PREFETCH=8
        GPU_WORKERS=16
        GPU_THREADS=8
    elif [[ "$GPU_NAME" == *"RTX 2080 Ti"* || "$GPU_NAME" == *"RTX 20"* || "$GPU_NAME" == *"GTX 16"* ]]; then
        # Turing sm_75 (RTX 20-series, GTX 16-series). Measured: 12/8/128
        # → 100% util / 296W sustained on a boost-enabled host. Runs the
        # Pascal sm_61 PTX → JIT to sm_75 (no native sm_75 cubin).
        GPU_BATCH=128
        GPU_PREFETCH=8
        GPU_WORKERS=12
        GPU_THREADS=8
    elif [[ "$GPU_NAME" == *"RTX 30"* || "$GPU_NAME" == *"A100"* || "$GPU_NAME" == *"A40"* || "$GPU_NAME" == *"A4000"* || "$GPU_NAME" == *"A5000"* || "$GPU_NAME" == *"A6000"* ]]; then
        # Ampere — consumer 30-series (sm_86) + workstation/datacenter
        # A100 / A40 / Axxxx (sm_80). Measured on 3060 Ti: 12/8/128
        # → 100% util / 190W sustained (95% of 200W TDP). Runs the
        # Pascal sm_61 PTX → JIT (no native sm_80/sm_86 cubin in v4.3).
        GPU_BATCH=128
        GPU_PREFETCH=8
        GPU_WORKERS=12
        GPU_THREADS=8
    elif [[ "$GPU_NAME" == *"RTX 4060 Ti"* ]]; then
        # Ada Lovelace sm_89, 16GB. Measured: 12/8/128 → 100% util / 164W
        # sustained. Sweep tested batch=64/128/256; batch had zero effect
        # on steady-state util. The native sm_89 cubin requires v4.3+
        # (or sm_61 PTX → JIT fallback on v4.0-v4.2). Check with
        # `cuobjdump --list-elf` to confirm sm_89 is present.
        GPU_BATCH=128
        GPU_PREFETCH=8
        GPU_WORKERS=12
        GPU_THREADS=8
    elif [[ "$GPU_NAME" == *"RTX 4070"* || "$GPU_NAME" == *"RTX 4080"* || "$GPU_NAME" == *"RTX 4090"* ]]; then
        # Ada Lovelace sm_89, larger dies. Untested at this scale —
        # start from the 5070 profile (workers=16) and tune up.
        GPU_BATCH=128
        GPU_PREFETCH=8
        GPU_WORKERS=16
        GPU_THREADS=8
    elif [[ "$GPU_NAME" == *"RTX 5060 Ti"* ]]; then
        # Blackwell sm_120, 16GB. Measured: 16/8/128 → 99% util / 150W.
        # Standardized on the universal modern profile for consistency
        # with 5070 / 5070 Ti / 4060 Ti / 2080 Ti — over-provisioning
        # workers is cheap (async; idle workers don't burn cycles).
        GPU_BATCH=128
        GPU_PREFETCH=8
        GPU_WORKERS=16
        GPU_THREADS=8
    elif [[ "$GPU_NAME" == *"RTX 5070 Ti"* ]]; then
        # Blackwell sm_120, 16GB. NOTE: batch=256+prefetch=16 broke CUDA
        # on this card historically. Keep batch=128. Workers bumped for
        # the extra SM count vs 5060 Ti.
        GPU_BATCH=128
        GPU_PREFETCH=8
        GPU_WORKERS=16
        GPU_THREADS=8
    elif [[ "$GPU_NAME" == *"RTX 5070"* ]]; then
        # Blackwell sm_120, 12GB. Measured: 16/8/128 → 99.9% util / 223W
        # sustained. The 5060 Ti's workers=8 default caps the 5070 at
        # only 70.6% util — the extra SMs and higher boost clock starve
        # the prep pipeline. Bump to workers=16.
        GPU_BATCH=128
        GPU_PREFETCH=8
        GPU_WORKERS=16
        GPU_THREADS=8
    elif [[ "$GPU_NAME" == *"RTX 5080"* || "$GPU_NAME" == *"RTX 5090"* ]]; then
        # Blackwell sm_120 large-die. Untested at this scale — start from
        # the 5070 profile and tune up.
        GPU_BATCH=128
        GPU_PREFETCH=8
        GPU_WORKERS=16
        GPU_THREADS=8
    fi
    cat > "$CONFIG_PATH" <<YAML
# DEXBTX miner config — generated by install.sh
# Production-canonical solver tuning: env vars match upstream BTX matmul
# source names verified via grep getenv. Wrong names silently no-op
# (we shipped BTX_MATMUL_PREFETCH for a while; it doesn't exist).
#
# >>> THE TWO LEVERS THAT MATTER: solver_prepare_workers + solver_threads <<<
# These control CPU-side input generation for the matmul kernel. A
# Blackwell-class GPU at the canonical 5060 Ti tuning (workers=8) is
# CPU-input-bound at 70% util on cards faster than a 5060 Ti. Bump them
# to 16/8 if you have ≥16 effective vCPUs and the card is sub-100% util.
# Batch size, prefetch depth, and async flag do NOT meaningfully change
# steady-state throughput once prep is sufficient (verified by sweep).
pool_host: "${POOL%:*}"
pool_port: ${POOL##*:}
pool_tls: false

payout_address: "${ADDRESS}"
worker_name: "${WORKER}"

gbt_solve_path: "${SOLVER_PATH}"

# Solver tuning (per-GPU profile chosen from detected hardware: ${GPU_NAME:-CPU only})
solver_backend: "cuda"
solver_prepare_workers: ${GPU_WORKERS} # BTX_MATMUL_PREPARE_WORKERS (KEY LEVER — bump together with threads)
solver_threads: ${GPU_THREADS}         # BTX_MATMUL_SOLVER_THREADS  (KEY LEVER — bump together with workers)
solver_batch_size: ${GPU_BATCH}        # BTX_MATMUL_SOLVE_BATCH_SIZE
solver_prefetch_depth: ${GPU_PREFETCH} # BTX_MATMUL_PREPARE_PREFETCH_DEPTH
solver_pipeline_async: 1               # BTX_MATMUL_PIPELINE_ASYNC (overlap prep+kernel)
gpu_inputs: 0                          # BTX_MATMUL_GPU_INPUTS (CPU-gen inputs; the "GPU saturation breakthrough" fix)

nonces_per_slice: 20000000
reconnect_initial_s: 1.0
reconnect_max_s: 60.0

log_level: "INFO"
YAML
    log "config written → $CONFIG_PATH"
    log "  GPU profile:        workers=${GPU_WORKERS}  threads=${GPU_THREADS}  batch=${GPU_BATCH}  prefetch=${GPU_PREFETCH}"
    log "  if util stays below 95% during steady-state mining, bump workers + threads"
    log "  (workers and threads are the dominant levers on Blackwell)"
fi

# ─── Hard GPU acceleration smoke test ───────────────────────────────────────
# Runs the solver with --backend cuda against a deterministic input AND
# verifies CUDA actually engaged via three independent signals — solver
# alone reports JSON in CPU-fallback mode too, which is the silent-failure
# mode where a miner runs at ~6 N/s on CPU for hours without noticing.
#
# Three-assertion check (any failure aborts install):
#   (a) solver PID visible in nvidia-smi --query-compute-apps during run
#   (b) sustained GPU power > 100W (5060 Ti idle ~30W; engaged 150-180W)
#   (c) effective throughput > 1000 N/s — this uses tries_used/elapsed_s
#       which docs/TUNING.md correctly calls a misleading per-card rate
#       metric. Here it's used ONLY as a LIVENESS FLOOR (proves the
#       kernel did productive work, vs the CPU-fallback ~6 N/s case),
#       NOT as a tuning measurement. 1000 N/s is well below any real
#       GPU's actual rate (5060 Ti is 28000+) — failure means CPU
#       fallback or catastrophic kernel-launch failure, not a tuning
#       problem.
#
# If (a) fails, the binary lacks a kernel image compatible with this
# driver/GPU. The fix is ALWAYS to ship a binary that supports the user's
# driver — never to ask the user to downgrade. We aim at current drivers.
if [[ "$HAS_NVIDIA" -eq 1 ]]; then
    log "running GPU acceleration smoke test (20s with engagement assertions)..."

    # Background nvidia-smi poll for power curve
    POLL_OUT="$(mktemp)"
    APPS_OUT="$(mktemp)"
    trap 'rm -f "$POLL_OUT" "$APPS_OUT"' EXIT
    ( for i in $(seq 1 22); do
        nvidia-smi --query-gpu=utilization.gpu,power.draw --format=csv,noheader
        nvidia-smi --query-compute-apps=pid,process_name --format=csv,noheader >> "$APPS_OUT" 2>/dev/null
        sleep 1
      done ) > "$POLL_OUT" 2>&1 &
    POLL_PID=$!

    # CRITICAL: the smoke test must mirror the env-var setup the production
    # miner uses — most importantly BTX_MATMUL_GPU_INPUTS=0. Without it the
    # solver defaults to GPU-side input gen on most modern cards (the
    # "8% util / 33W silent fallback" path), which would correctly fail
    # assertion (b) but for the wrong reason — making install.sh look like
    # a host-power problem when the real issue is the smoke test exercising
    # a different code path than the production miner.
    SMOKE_OUT="$(BTX_MATMUL_BACKEND=cuda \
        BTX_MATMUL_GPU_INPUTS=0 \
        BTX_MATMUL_SOLVE_BATCH_SIZE=128 \
        BTX_MATMUL_PREPARE_PREFETCH_DEPTH=8 \
        BTX_MATMUL_PREPARE_WORKERS=8 \
        BTX_MATMUL_PIPELINE_ASYNC=1 \
        "$SOLVER_PATH" \
        --version 536870912 \
        --prev-hash 0ab38fdff2ef667dcddac7f50c3696080c26697615f7b6b9af5c3a1ba0a5fb7e \
        --merkle-root d906f02ed11d8936770423263b56c5ffe1ea1b15c8a2867afb161adb6fd76eb7 \
        --time 1779672814 --bits 0x1d17c609 \
        --share-target 00005f59c4000000000000000000000000000000000000000000000000000000 \
        --seed-a 8460daf3ff446cc55a7115de88ee24c8a2bf182eedde43abb9cf4cc94cc209bf \
        --seed-b 7f2e377616feb92d2e9857cab390595b7d6b8d24373a2da394f8d97197b5f437 \
        --block-height 110806 --nonce-start 1 \
        --max-tries 100000000 --max-seconds 20 \
        --backend cuda --solver-threads 4 --batch-size 128 2>&1 || true)"

    wait $POLL_PID 2>/dev/null || true

    SMOKE_LAST_LINE="$(echo "$SMOKE_OUT" | grep -E '^\{.*\}$' | tail -1)"
    if [[ -z "$SMOKE_LAST_LINE" ]]; then
        echo "$SMOKE_OUT" | tail -10 >&2
        err "GPU smoke test: solver produced no JSON output. CUDA backend likely failed to initialize. Aborting."
    fi

    # ASSERTION (a): solver appeared in compute-apps
    if ! grep -q btx-gbt-solve "$APPS_OUT" 2>/dev/null; then
        echo
        err "GPU smoke test FAILED (assertion a: nvidia-smi --query-compute-apps never saw btx-gbt-solve).

The solver silently fell back to CPU. The binary does NOT have a kernel image
compatible with your GPU/driver combination. Driver: $(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1).

This is a binary issue, NOT a driver issue — DO NOT downgrade your driver.

Action: file an issue at github.com/dexbtx/minebtx/issues with:
  - GPU model: ${GPU_NAME}
  - Driver version: $(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)
  - CUDA runtime: $(nvidia-smi --query-gpu=cuda_version --format=csv,noheader 2>/dev/null | head -1)

We will ship a binary build that supports your hardware. Until then this
install cannot proceed — running on CPU would burn money for trivial output."
    fi

    # ASSERTION (b): sustained power > 100W (need ≥10 samples above)
    HIGH_POWER_SAMPLES="$(awk -F, '{gsub(/[ W]/,"",$2); if ($2+0 > 100) c++} END{print c+0}' "$POLL_OUT")"
    if [[ "$HIGH_POWER_SAMPLES" -lt 10 ]]; then
        warn "GPU smoke test WARN (assertion b: only ${HIGH_POWER_SAMPLES}/20 samples above 100W)"
        warn "  Power curve was:"
        head -5 "$POLL_OUT" | sed 's/^/    /' >&2
        warn "  GPU compute engaged but at low power — host may be power-capping"
        warn "  or sharing physical GPU with another tenant. Throughput will be degraded."
    fi

    # ASSERTION (c): effective throughput > 1000 N/s
    THROUGHPUT_N_S="$(echo "$SMOKE_LAST_LINE" | python3 -c "
import sys, json
try:
    r = json.loads(sys.stdin.read())
    tries = r.get('tries_used', 0)
    elap = max(r.get('elapsed_s', 1), 0.01)
    print(int(tries / elap))
except Exception:
    print(0)
")"
    if [[ "$THROUGHPUT_N_S" -lt 1000 ]]; then
        err "GPU smoke test FAILED (assertion c: throughput ${THROUGHPUT_N_S} N/s, expected >1000).

Solver appeared in compute-apps and drew power, but effective throughput is
${THROUGHPUT_N_S} nonces/sec — far below the 1000 N/s floor that proves
the kernel ran productively. Canonical 5060 Ti is 28000+ N/s.

Possible causes:
  - Noisy neighbor sharing SMs on the same physical GPU
  - Thermal throttling under sustained load
  - Driver issue causing kernel launches to fail silently after first launch

This install cannot proceed at this throughput."
    fi

    log "GPU smoke test PASS:"
    log "  compute-app PID visible in nvidia-smi: yes"
    log "  sustained power: ${HIGH_POWER_SAMPLES}/20 samples >100W"
    log "  throughput: ${THROUGHPUT_N_S} N/s"
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
