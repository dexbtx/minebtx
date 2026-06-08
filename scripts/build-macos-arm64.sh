#!/usr/bin/env bash
# Local macOS arm64 + Metal build of btx-gbt-solve — same recipe as
# .github/workflows/build-solver-macos-arm64.yml, but for fast local iteration
# on an Apple Silicon Mac. Run from the root of a dexbtx/minebtx checkout
# (it reads .github/build-patches/).
#
#   bash scripts/build-macos-arm64.sh                # build + verify
#   BTX_TAG=v0.32.2 bash scripts/build-macos-arm64.sh
#
# On success it prints the built binary path + its sha256, and confirms the
# Metal kernel is bit-equivalent to the production solver past block 125000.
set -uo pipefail

BTX_TAG="${BTX_TAG:-v0.32.2}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PATCH_DIR="$REPO_ROOT/.github/build-patches"
WORK="${WORK:-$REPO_ROOT/.macbuild}"
SRC="$WORK/btx-src"

log()  { printf '\033[1;34m[build]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || err "this script is for macOS (Darwin) only"
[ "$(uname -m)" = "arm64" ]  || err "this script is for Apple Silicon (arm64) only"
[ -d "$PATCH_DIR" ] || err "run from a dexbtx/minebtx checkout — missing $PATCH_DIR"

# ── Prereqs ──────────────────────────────────────────────────────────────────
command -v xcrun >/dev/null 2>&1 || err "Xcode command line tools missing — run: xcode-select --install"
command -v brew  >/dev/null 2>&1 || err "Homebrew missing — see https://brew.sh"
log "installing build deps via brew (idempotent)…"
brew install cmake boost libevent pkgconf ninja >/dev/null || err "brew install failed"
log "xcrun metal: $(xcrun -sdk macosx metal --version 2>&1 | head -1)"

# ── Source + patches ─────────────────────────────────────────────────────────
mkdir -p "$WORK"
if [ ! -d "$SRC/.git" ]; then
  log "cloning btxchain/btx @ $BTX_TAG …"
  git clone --depth 1 --branch "$BTX_TAG" https://github.com/btxchain/btx "$SRC" || err "clone failed"
else
  log "reusing existing source at $SRC"
fi
log "btx source HEAD: $(git -C "$SRC" rev-parse --short HEAD)"

log "applying dexbtx patches (02/03/05 — skipping 01, CUDA-only)…"
( cd "$SRC"
  # Reset tracked files so re-runs apply cleanly.
  git checkout -- . 2>/dev/null || true
  for p in 02-pow-h-share-target-override.patch \
           03-pow-cpp-share-target-override.patch \
           05-cmakelists-add-gbt-solve-target.patch; do
    log "  patch $p"
    patch -p1 < "$PATCH_DIR/$p" || err "failed to apply $p"
  done
  cp "$PATCH_DIR/btx-gbt-solve.cpp" src/btx-gbt-solve.cpp
  grep -q "nMatMulNonceSeedHeight = 125'000" src/btx-gbt-solve.cpp \
    || err "V2 activation height not set in solver source"
) || exit 1

# ── Configure + build ────────────────────────────────────────────────────────
log "configuring (Metal on, CUDA off)…"
cmake -S "$SRC" -B "$SRC/build" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INTERPROCEDURAL_OPTIMIZATION=ON \
  -DBTX_ENABLE_CUDA_EXPERIMENTAL=OFF \
  -DBTX_ENABLE_METAL=ON \
  -DBTX_MATMUL_METAL_PRECOMPILE_KERNELS=ON \
  -DBUILD_GUI=OFF -DBUILD_TESTS=OFF -DBUILD_BENCH=OFF \
  -DBUILD_FUZZ_BINARY=OFF -DBUILD_UTIL=ON \
  || err "cmake configure failed"

log "building btx-gbt-solve + btx-matmul-backend-info (this is the slow part)…"
cmake --build "$SRC/build" --target btx-gbt-solve btx-matmul-backend-info -j "$(sysctl -n hw.ncpu)" \
  || err "build failed"

BIN="$(find "$SRC/build" -name btx-gbt-solve -type f -perm +111 | head -1)"
INFO="$(find "$SRC/build" -name btx-matmul-backend-info -type f | head -1)"
[ -n "$BIN" ] || err "built binary not found"
log "binary: $BIN"
file "$BIN"; lipo -archs "$BIN" 2>/dev/null || true
otool -L "$BIN" | grep -iE "Metal|Foundation" || log "WARN: Metal/Foundation not in otool -L"

# ── Verify: Metal available + bit-equivalent to reference ────────────────────
log "checking Metal backend is available…"
"$INFO" 2>&1 | tee "$WORK/backend-info.txt" | grep -iE "metal" | grep -ivE "unavailable|not available|disabled" \
  || err "Metal backend not reported available (would silently CPU-fallback)"

REF="$PATCH_DIR/ci-refvec.json"
EXP="$(python3 -c "import json;print(json.load(open('$REF'))['expected']['matmul_digest'])")"
VEC="$(python3 -c "import json;print(json.dumps(json.load(open('$REF'))['job']))")"
log "reference digest: $EXP"
run() { echo "$VEC" | BTX_MATMUL_BACKEND="$1" "$BIN" --daemon --backend "$1" --solver-threads 1 2>/dev/null \
        | python3 -c "import sys,json;print(json.load(sys.stdin).get('matmul_digest','NONE'))"; }
CPU="$(run cpu)";   log "cpu   digest: $CPU"
METAL="$(run metal)"; log "metal digest: $METAL"
[ "$CPU" = "$EXP" ]   || err "CPU digest mismatch — kernel/source wrong"
[ "$METAL" = "$EXP" ] || err "Metal digest mismatch — Metal kernel NOT consensus-correct"

SHA="$(shasum -a 256 "$BIN" | awk '{print $1}')"
printf '\n\033[1;32m[PASS]\033[0m cpu == metal == reference. Metal kernel is consensus-correct post-125000.\n'
printf '  binary: %s\n  sha256: %s\n' "$BIN" "$SHA"
printf '  -> pin this as DARWIN_ARM64_SHA256 in install.sh and publish it as btx-gbt-solve-darwin-arm64\n'
