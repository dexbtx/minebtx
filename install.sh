#!/usr/bin/env bash
# DEXBTX miner — one-line installer.
#
# Usage:
#   curl -fsSL https://minebtx.com/install.sh | bash
#   curl -fsSL https://minebtx.com/install.sh | bash -s -- --address btx1z...
#
# What this script does:
#   0. Self-update: re-fetch the latest install.sh from the repo and re-exec
#      if our local copy is older. Skip with DEXBTX_NO_SELFUPDATE=1.
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

# ─── Self-update bootstrap ──────────────────────────────────────────────────
# Marker — keep this exact line + bump on each release. The bootstrap
# downstream parses this string and skips re-exec if it matches.
INSTALL_SH_VERSION="0.3.15"

INSTALL_SH_LATEST_URL="https://github.com/dexbtx/minebtx/raw/main/install.sh"

if [[ "${DEXBTX_NO_SELFUPDATE:-0}" != "1" ]]; then
    _bootstrap_tmp="$(mktemp /tmp/dexbtx-install-sh.XXXXXX 2>/dev/null || mktemp)"
    if [[ -n "$_bootstrap_tmp" ]] && \
        curl -fsSL --connect-timeout 5 --max-time 30 \
            "$INSTALL_SH_LATEST_URL" -o "$_bootstrap_tmp" 2>/dev/null; then
        _latest_ver="$(grep -E '^INSTALL_SH_VERSION=' "$_bootstrap_tmp" \
            | head -1 | cut -d= -f2 | tr -d '"' | tr -d "'")"
        if [[ -n "$_latest_ver" && "$_latest_ver" != "$INSTALL_SH_VERSION" ]]; then
            echo "[install] self-updating $INSTALL_SH_VERSION -> $_latest_ver"
            chmod +x "$_bootstrap_tmp"
            DEXBTX_NO_SELFUPDATE=1 exec bash "$_bootstrap_tmp" "$@"
        fi
        rm -f "$_bootstrap_tmp"
    else
        # GitHub raw unreachable; warn but continue with the local copy.
        echo "[install] (self-update check skipped: $INSTALL_SH_LATEST_URL unreachable)" >&2
    fi
fi
unset _bootstrap_tmp _latest_ver

set -euo pipefail

# ─── Configurables ──────────────────────────────────────────────────────────
# Pin the prebuilds release tag. install.sh always pulls this version.
# Bump in lockstep with experiments/vast/prebuilds and pyproject.toml.
PREBUILDS_TAG="${PREBUILDS_TAG:-btx-prebuilds-v0.32.11-preempt}"
EXPECTED_SHA256="${EXPECTED_SHA256:-70f16afdd0be23cbf94858196d9fa5d1c8fade46c1fbaa06dcc45ba60fa99a94}"
# Darwin arm64 (Apple Silicon + Metal) solver pin. Fill in after the first green
# build-solver-macos-arm64 CI run (the workflow prints the sha256). Until then,
# macOS installs intentionally fail rather than install an unverified binary.
DARWIN_ARM64_SHA256="${DARWIN_ARM64_SHA256:-361abdad3880fe8be4ff470c29238c90303c6bd78dcac3b15643607fc369002c}"
# Linux aarch64 (Grace / GB10 Blackwell etc.) CUDA solver pins. Default CUDA
# toolkit variant is cuda12; set DEXBTX_CUDA=cuda13 for newer-driver hosts.
AARCH64_CUDA12_SHA256="${AARCH64_CUDA12_SHA256:-72f083c22704dcac683ac324bd2183ccb2c35a7fa60d847345c15757f3f0b625}"
AARCH64_CUDA13_SHA256="${AARCH64_CUDA13_SHA256:-a8d3728b871cf200abbcbd3305e2b1282849e015f4b2b0cbaea31259d9858572}"
# Linux x86_64 AMD/ROCm (HIP) solver pin — EXPERIMENTAL. Selected when an AMD
# GPU is present (rocm-smi) or DEXBTX_GPU=rocm. Correctness is enforced by an
# install-time HIP-vs-CPU self-check below (the HIP kernel is unproven off real
# AMD silicon). The reference digest for that self-check:
ROCM_X86_64_SHA256="${ROCM_X86_64_SHA256:-65d178631b4378a7d474d0ecec7609ee01125ccc34770caaf615d898f63ed049}"
SELFCHECK_REF_DIGEST="7db2e9351c8c947293cb12d086ff03435730156265b67e3bce9dab1956074b14"
PREBUILDS_BASE="${PREBUILDS_BASE:-https://github.com/dexbtx/minebtx/releases/download/${PREBUILDS_TAG}}"
# Default asset = Linux x86_64; the Darwin branch below overrides for Apple Silicon.
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
ARCH="$(uname -m)"
IS_ROCM=0
case "$OS" in
    Linux)
        # x86_64: default CUDA/CPU asset, unless an AMD GPU is present (ROCm/HIP,
        # experimental). aarch64 (Grace/GB10) needs its own ARM64 CUDA build, or
        # install.sh would hand an ARM host an x86_64 binary that can't exec.
        case "$ARCH" in
            x86_64|amd64)
                if [[ "${DEXBTX_GPU:-}" == "rocm" ]] || command -v rocm-smi >/dev/null 2>&1; then
                    # The experimental ROCm/HIP build is NOT published for the
                    # v0.32.11 (MatMul-V3) release — its build workflow still
                    # carries the pre-v0.32.8 PR#58 patch set and fails. Rather
                    # than 404 mid-download, fail clearly. AMD is a minority,
                    # experimental path; the NVIDIA/CPU x86_64 build below is the
                    # supported one.
                    err "AMD/ROCm has no prebuilt solver for the v0.32.11 MatMul-V3 release yet. \
Options: (1) force the NVIDIA/CPU x86_64 build with DEXBTX_GPU=none, or (2) build btx-gbt-solve from source against btxchain/btx v0.32.11. ROCm support will return in a later point release."
                fi
                ;;
            aarch64|arm64)
                CUDA_VARIANT="${DEXBTX_CUDA:-cuda12}"
                log "Linux aarch64 detected — using the ARM64 ${CUDA_VARIANT} solver build."
                SOLVER_URL="${PREBUILDS_BASE}/btx-gbt-solve-aarch64-linux-gnu-${CUDA_VARIANT}"
                if [[ "$CUDA_VARIANT" == "cuda13" ]]; then
                    EXPECTED_SHA256="${AARCH64_CUDA13_SHA256}"
                else
                    EXPECTED_SHA256="${AARCH64_CUDA12_SHA256}"
                fi
                ;;
            *) err "unsupported Linux architecture: $ARCH (published builds: x86_64, aarch64)" ;;
        esac
        ;;
    Darwin)
        # Apple Silicon only — the published Mac build is arm64 + Metal. There is
        # no Intel (x86_64) macOS solver.
        if [[ "$ARCH" != "arm64" ]]; then
            err "macOS Intel (x86_64) is not supported — Apple Silicon (arm64) only."
        fi
        log "macOS Apple Silicon detected — using the Metal solver build (no NVIDIA path)."
        SOLVER_URL="${PREBUILDS_BASE}/btx-gbt-solve-darwin-arm64"
        EXPECTED_SHA256="${DARWIN_ARM64_SHA256}"
        if [[ "$EXPECTED_SHA256" == "REPLACE_AFTER_FIRST_MACOS_BUILD" ]]; then
            err "macOS solver SHA pin not set yet. Run the build-solver-macos-arm64 workflow, publish the asset, then set DARWIN_ARM64_SHA256 (in install.sh or via env)."
        fi
        ;;
    *)      err "unsupported OS: $OS" ;;
esac

# GPU detection
HAS_NVIDIA=0
GPU_NAME=""
if [[ "$OS" == "Darwin" ]]; then
    # Apple Silicon uses the Metal backend. The NVIDIA / CPU-fallback check
    # below is Linux-only — running it on macOS would wrongly warn "CPU only"
    # for what is actually a Metal-accelerated build.
    GPU_NAME="Apple Silicon (Metal)"
elif [[ "$IS_ROCM" -eq 1 ]]; then
    # AMD/ROCm — the HIP backend presents as "cuda"; skip the NVIDIA/CPU check.
    GPU_NAME="AMD GPU (ROCm/HIP, experimental)"
else
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
fi

# Python
need curl
# SHA-256 helper — Linux has sha256sum; macOS ships shasum instead.
if command -v sha256sum >/dev/null 2>&1; then
    sha256_of() { sha256sum "$1" | awk '{print $1}'; }
elif command -v shasum >/dev/null 2>&1; then
    sha256_of() { shasum -a 256 "$1" | awk '{print $1}'; }
else
    err "no SHA-256 tool found (need sha256sum or shasum)"
fi

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
    if [[ "$OS" == "Darwin" ]]; then
        command -v brew >/dev/null 2>&1 || err "no python3.10+ found and Homebrew missing — install Python 3.10+ (see https://brew.sh, then 'brew install python') and re-run"
        log "installing python@3.11 via brew..."
        brew install python@3.11 || err "brew install python@3.11 failed"
        PYTHON="$(brew --prefix)/opt/python@3.11/bin/python3.11"
        command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python3.11
    elif command -v apt-get >/dev/null 2>&1; then
        log "installing python3.11 via apt..."
        sudo apt-get update -qq
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3.11 python3.11-venv python3-pip
        PYTHON=python3.11
    else
        err "no python3.10+ found and no supported package manager (apt/brew) — install Python 3.10+ manually then re-run"
    fi
fi
log "using Python: $($PYTHON --version 2>&1)"
# Where `pip install --user` drops console scripts (Linux: ~/.local/bin;
# macOS: ~/Library/Python/X.Y/bin). Used for the PATH hint + launch command.
USER_BIN="$("$PYTHON" -m site --user-base 2>/dev/null)/bin"

# ─── Install pip + runtime deps + dexbtx-miner ──────────────────────────────
# Many vast.ai CUDA images ship without pip — install it via apt if missing.
if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
    if [[ "$OS" == "Darwin" ]]; then
        log "bootstrapping pip via ensurepip..."
        "$PYTHON" -m ensurepip --upgrade 2>/dev/null || err "pip missing — try 'brew install python@3.11' then re-run"
    elif command -v apt-get >/dev/null 2>&1; then
        log "python pip not present; installing via apt..."
        sudo apt-get update -qq
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-pip
    else
        err "python pip missing and apt-get not available; install pip manually then re-run"
    fi
fi

# pip install wrapper — retries with --break-system-packages on PEP-668
# ("externally-managed-environment"), which Homebrew/system Python on macOS
# (and newer Debian) raise for --user installs.
pip_install() {
    local elog; elog="$(mktemp)"
    if "$PYTHON" -m pip install --user "$@" 2>"$elog"; then
        rm -f "$elog"; return 0
    fi
    if grep -q "externally-managed-environment" "$elog"; then
        warn "PEP-668 externally-managed env — retrying with --break-system-packages"
        rm -f "$elog"
        "$PYTHON" -m pip install --user --break-system-packages "$@"
    else
        cat "$elog" >&2; rm -f "$elog"; return 1
    fi
}

# Runtime deps (pyyaml for --config). Install regardless of --skip-pip
# because --skip-pip only skips the dexbtx-miner package itself (useful for
# source-tree dev), not its transitive deps.
log "installing runtime deps (pyyaml)..."
pip_install --quiet --upgrade pyyaml

if [[ "$SKIP_PIP" -eq 1 ]]; then
    log "skipping dexbtx-miner pip install (--skip-pip); assuming source tree is on PYTHONPATH"
else
    # Install the Python package directly from the GitHub source tarball
    # for the v0.3 release tag. We do NOT publish to PyPI — fetching from
    # GitHub keeps the install pinned to a specific release commit and
    # avoids a third-party package surface. Override DEXBTX_MINER_PKG_URL
    # to install from a fork or a different ref.
    DEXBTX_MINER_PKG_URL="${DEXBTX_MINER_PKG_URL:-https://github.com/dexbtx/minebtx/archive/refs/tags/v0.4.17.tar.gz}"
    log "installing dexbtx-miner from ${DEXBTX_MINER_PKG_URL} (pip --user)..."
    pip_install --upgrade "$DEXBTX_MINER_PKG_URL"

    # Make sure the pip --user bin dir is on PATH for the next session.
    case ":$PATH:" in
        *":$USER_BIN:"*) : ;;
        *) warn "dexbtx-miner was installed to $USER_BIN (not on PATH). Add to your shell rc: export PATH=\"$USER_BIN:\$PATH\"" ;;
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

ACTUAL_SHA="$(sha256_of "$TMP")"
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
elif echo "$HELP_OUT" | grep -qiE "error while loading shared libraries|cannot open shared object|libcudart"; then
    # The binary couldn't even load — almost always a missing CUDA runtime
    # (e.g. libcudart.so.12 absent on a CUDA-13-only host). This is NOT a
    # "wrong binary" problem; --help never ran. v0.32.11+ solvers are now
    # statically linked and shouldn't hit this.
    MISSING="$(echo "$HELP_OUT" | grep -oE 'lib[a-zA-Z0-9._-]+\.so[0-9.]*' | head -1)"
    echo "$HELP_OUT" | tail -3 >&2
    err "solver binary failed to LOAD (missing ${MISSING:-a shared library}), so its flags couldn't be read. This is a CUDA-runtime mismatch, not a wrong binary. Fix: re-run install to pull the latest statically-linked solver, or install the matching CUDA runtime (e.g. 'pip install nvidia-cuda-runtime-cu12' + add its lib dir to LD_LIBRARY_PATH). Aborting."
else
    err "installed solver lacks the --share-target flag (the patch this release needs). --help output: $(echo "$HELP_OUT" | head -2 | tr '\n' ' '). Aborting."
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

# Solver backend + thread defaults from platform.
#   Apple Silicon: MLX (Apple's tuned array lib) edges the bespoke Metal
#   kernels by ~3-4% and is digest-equivalent (valid work either way). The
#   solver is CPU-prep-bound, so default ALL cores — on an M4, threads=4 left
#   ~50% of throughput on the table vs threads=ncpu (measured ~70 kN/s ->
#   ~133 kN/s). Fall back to "metal" in the config if MLX ever misbehaves.
#   NVIDIA: CUDA, threads=4 (prep-workers are the lever there, not threads).
#   Otherwise: CPU on all cores.
if [[ "$OS" == "Darwin" ]]; then
    SOLVER_BACKEND="mlx"
    SOLVER_THREADS="$(sysctl -n hw.ncpu 2>/dev/null || echo 4)"
elif [[ "$IS_ROCM" -eq 1 ]]; then
    # HIP masquerades as backend "cuda"; verified/possibly-downgraded by the
    # self-check below. Threads=4 (the GPU does the work).
    SOLVER_BACKEND="cuda"
    SOLVER_THREADS=4
elif [[ "$HAS_NVIDIA" -eq 1 ]]; then
    SOLVER_BACKEND="cuda"
    # v0.4.x — GPU-class-aware defaults from the 48h / 550-worker pool analysis.
    # The canonical winner across most cards (3060-4090, 5070/5080) is
    # SOLVER_THREADS=8 / PREPARE_WORKERS=16. We do NOT scale threads to nproc
    # anymore: over-threading STARVES fast cards (a 4090 drops 95%->68% at
    # THREADS=24; THREADS=24 + big batch is the single most common
    # underutilization trap on the network). Exceptions: slower cards want a
    # heavier CPU feed (16); the RTX 5090 wants 12/24 AND a dedicated, high-clock
    # CPU host (a 6-core Ryzen feeds it to 89%; a shared/oversubscribed cloud
    # EPYC stalls it ~70% regardless of config — that's a host choice, not tuning).
    NPROC="$(nproc 2>/dev/null || echo 8)"
    _gpu_uc="$(printf '%s' "$GPU_NAME" | tr '[:lower:]' '[:upper:]')"
    if printf '%s' "$_gpu_uc" | grep -qE '5090|PRO 6000'; then
        SOLVER_THREADS=12; GPU_WORKERS=24
    elif printf '%s' "$_gpu_uc" | grep -qE '5060|4060|3060|3070|1060|1070|1080|LAPTOP'; then
        SOLVER_THREADS=16; GPU_WORKERS=24   # slower cards: heavier CPU feed helps
    else
        SOLVER_THREADS=8; GPU_WORKERS=16    # canonical winner (4090/4080/5080/5070/4070/3090/3080/...)
    fi
    # Floor to the host's thread budget on small machines.
    _budget=$(( NPROC - 2 < 4 ? 4 : NPROC - 2 ))
    [ "$SOLVER_THREADS" -gt "$_budget" ] && SOLVER_THREADS="$_budget"
else
    SOLVER_BACKEND="cpu"
    SOLVER_THREADS="$(nproc 2>/dev/null || echo 4)"
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
    # v0.4.4 — empirically-found optimal across the GPU-config sweep on
    # home-1070 (GTX 1070, Pascal sm_61). Pre-v0.4.4 we set Pascal-specific
    # batch=32/prefetch=4/workers=4 thinking it was the "Pascal sweet spot,"
    # but the sweep showed batch=128/prefetch=8/workers=16 works on Pascal
    # too — the actual lever is solver_threads (handled above, auto-scaled
    # from nproc). batch=128/workers=16/prefetch=8 + threads=24 hit
    # 108 kN/s on the 1070 (was 30 kN/s at the old "Pascal sweet spot").
    # Modern cards retain the same shape and benefit from the same
    # parallelism — sweep across hardware classes hasn't shown a card that
    # PREFERS batch<128, so we set one default that works fleet-wide.
    GPU_BATCH=128
    GPU_PREFETCH=8
    GPU_WORKERS="${GPU_WORKERS:-16}"   # GPU-class-aware value set above (24 for slow cards / 5090); 16 default
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
solver_backend: "${SOLVER_BACKEND}"   # Apple Silicon: "mlx" (fastest) or "metal"; NVIDIA: "cuda"; else "cpu"
solver_threads: ${SOLVER_THREADS}      # solver is prep-bound — all cores on Apple Silicon / CPU
solver_batch_size: ${GPU_BATCH}        # BTX_MATMUL_SOLVE_BATCH_SIZE
solver_prefetch_depth: ${GPU_PREFETCH} # BTX_MATMUL_PREPARE_PREFETCH_DEPTH
solver_prepare_workers: ${GPU_WORKERS} # BTX_MATMUL_PREPARE_WORKERS
solver_pipeline_async: 1               # BTX_MATMUL_PIPELINE_ASYNC (overlap prep+kernel)
gpu_inputs: 1                          # BTX_MATMUL_GPU_INPUTS (GPU-gen inputs; MANDATORY post-block-125000 for saturation on all cards; 0 was pre-fork)

nonces_per_slice: 2000000
reconnect_initial_s: 1.0
reconnect_max_s: 60.0

log_level: "INFO"
YAML
    log "config written → $CONFIG_PATH (profile: batch=${GPU_BATCH} prefetch=${GPU_PREFETCH} workers=${GPU_WORKERS})"
fi

# ─── AMD/ROCm HIP correctness self-check ─────────────────────────────────────
# The HIP kernel is EXPERIMENTAL and cannot be verified without real AMD
# hardware (CI has no AMD GPU). So verify it HERE, on the user's actual GPU:
# run the deterministic post-125000 V2 reference vector through both the HIP
# backend ("cuda") and the CPU backend and require both to equal the known
# reference digest. On mismatch we downgrade the config to CPU mining rather
# than let it submit shares the pool will reject.
if [[ "$IS_ROCM" -eq 1 ]]; then
    log "running AMD HIP-vs-CPU correctness self-check on your GPU (experimental backend)..."
    SC_VEC='{"version":536870912,"prev_hash":"00000000000000000000000000000000000000000000000000000000000000ab","merkle_root":"00000000000000000000000000000000000000000000000000000000000000cd","time":1780000000,"bits":"207fffff","seed_a":"1111111111111111111111111111111111111111111111111111111111111111","seed_b":"2222222222222222222222222222222222222222222222222222222222222222","block_height":130000,"nonce_start":0,"max_tries":256,"max_seconds":60,"share_target":"ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"}'
    _digest() { echo "$SC_VEC" | BTX_MATMUL_BACKEND="$1" "$SOLVER_PATH" --daemon --backend "$1" --solver-threads 1 2>/dev/null \
        | grep -oE '"matmul_digest"[^0-9a-f]*[0-9a-f]{64}' | grep -oE '[0-9a-f]{64}' | head -1; }
    SC_CPU="$(_digest cpu)"
    SC_HIP="$(_digest cuda)"   # "cuda" == HIP in this build
    if [[ "$SC_HIP" == "$SELFCHECK_REF_DIGEST" && "$SC_CPU" == "$SELFCHECK_REF_DIGEST" ]]; then
        log "self-check PASS ✓ HIP digest == CPU == reference — AMD backend is consensus-correct on this GPU."
    else
        warn "self-check FAILED — the experimental HIP/ROCm kernel is NOT consensus-correct on this GPU."
        warn "  cpu=${SC_CPU:-<none>}  hip=${SC_HIP:-<none>}  ref=${SELFCHECK_REF_DIGEST}"
        warn "  Downgrading to CPU mining (solver_backend: cpu) so you don't submit rejected shares."
        warn "  Please report your GPU model + gfx arch so the HIP kernel can be fixed (likely the"
        warn "  wavefront-size / __shfl_down_sync path). GPU mining stays disabled until then."
        if [[ -f "$CONFIG_PATH" ]]; then
            sed -i.bak 's/^solver_backend:.*/solver_backend: "cpu"   # auto-downgraded: HIP self-check failed/' "$CONFIG_PATH" 2>/dev/null \
              || sed -i '' 's/^solver_backend:.*/solver_backend: "cpu"/' "$CONFIG_PATH" 2>/dev/null || true
        fi
    fi
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
# Resolve the launch command: bare name if it's on PATH, otherwise the full
# pip --user path so the printed command actually works (esp. on macOS where
# ~/Library/Python/X.Y/bin is rarely on PATH).
if command -v dexbtx-miner >/dev/null 2>&1; then
    MINER_CMD="dexbtx-miner"
else
    MINER_CMD="${USER_BIN}/dexbtx-miner"
fi

echo
log "✓ DEXBTX miner installed."
echo
echo "  Pool:     ${POOL}"
echo "  Address:  ${ADDRESS:-<edit ${CONFIG_PATH} and set payout_address>}"
echo "  Worker:   ${WORKER}"
echo "  GPU:      ${GPU_NAME:-CPU only}"
echo "  Backend:  ${SOLVER_BACKEND}"
echo
echo "Launch the miner:"
echo "  ${MINER_CMD} --config ${CONFIG_PATH}"
echo
echo "Or, for a long-running daemon (recommended):"
echo "  tmux new -d -s dexbtx '${MINER_CMD} --config ${CONFIG_PATH} 2>&1 | tee -a ${INSTALL_DIR}/miner.log'"
echo "  tmux attach -t dexbtx"
echo
echo "Stats + payouts via Telegram: @btxdexbot   /stats /mybalance /help"
echo
echo "Tune for your specific hardware (the defaults are a starting point):"
echo "  ${MINER_CMD} benchmark                  # 2-min sweep across common batch sizes"
echo "  ${MINER_CMD} benchmark --write-config   # write the winning config"
echo "See TUNING.md for the env-var reference + per-GPU rough guidelines."
echo
