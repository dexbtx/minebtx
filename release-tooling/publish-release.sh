#!/bin/bash
# Helper to publish prebuilt BTX artifacts as a GitHub release.
# Requires: gh CLI installed and authenticated (`gh auth login`).
#
# Usage:
#   ./publish-release.sh <repo-owner/repo-name> <tag> [release-title]
#   example: ./publish-release.sh dexbtx/minebtx v4.3-sm89-native
#
# After this runs successfully, the install.sh in the parent dir will
# fetch the freshly-uploaded btx-gbt-solve binary from this release.
# Update install.sh's PREBUILDS_TAG + EXPECTED_SHA256 in lockstep.
set -euo pipefail

if [ $# -lt 2 ]; then
    echo "usage: $0 <repo-owner/repo-name> <tag> [title]"
    echo "  example: $0 dexbtx/minebtx v4.3-sm89-native"
    exit 1
fi

REPO=$1
TAG=$2
TITLE=${3:-"BTX prebuilds $TAG"}

cd "$(dirname "$0")"

# Verify all expected files are present
for f in btxd btx-cli btx-gbt-solve btx-gbt-miner.py SHA256SUMS README.md; do
    if [ ! -f "$f" ]; then
        echo "FATAL: missing $f in $(pwd)"
        exit 1
    fi
done

# Verify integrity before publishing
echo "=== verifying SHA256SUMS ==="
sha256sum -c SHA256SUMS

# Verify the solver actually contains CUDA device code.
# 2026-05-24 incident: v4.0 was published with BTX_ENABLE_CUDA_EXPERIMENTAL=OFF
# in the cmake cache → binary had ZERO cubins / PTX → all GPU users silently
# CPU-fellback. See memory feedback_btx_v4_0_build_forgot_cuda_flag.md.
CUOBJDUMP="${CUOBJDUMP:-/usr/local/cuda-12.6/bin/cuobjdump}"
if [ ! -x "$CUOBJDUMP" ]; then
    echo "WARN: cuobjdump not found at $CUOBJDUMP; skipping CUDA device-code check"
    echo "      set CUOBJDUMP=... or install cuda-toolkit. Continuing at your risk."
else
    echo "=== verifying btx-gbt-solve contains CUDA device code ==="
    ELF_OUT=$("$CUOBJDUMP" --list-elf btx-gbt-solve 2>&1)
    PTX_OUT=$("$CUOBJDUMP" --list-ptx btx-gbt-solve 2>&1)
    echo "$ELF_OUT" | head -5
    echo "$PTX_OUT" | head -5
    echo "$ELF_OUT" | grep -q "sm_61" || { echo "FATAL: no sm_61 cubin in btx-gbt-solve (Pascal users would fall back to CPU)"; exit 1; }
    echo "$ELF_OUT" | grep -q "sm_90" || { echo "FATAL: no sm_90 cubin in btx-gbt-solve (Hopper/Blackwell PTX-JIT base missing)"; exit 1; }
    echo "  PASS: solver has sm_61 + sm_90 device code embedded"
fi

# Bundle the daemon binaries (smaller download than separate files)
echo "=== creating btx-daemon bundle ==="
tar czf btx-daemon.tar.gz btxd btx-cli
ls -lh btx-daemon.tar.gz

# Update SHA256SUMS to include the bundle. Strip any prior bundle line
# first so re-running this script on the same checkout doesn't append
# duplicates.
sed -i '/btx-daemon\.tar\.gz$/d' SHA256SUMS
sha256sum btx-daemon.tar.gz >> SHA256SUMS

# Compose release notes
NOTES=$(cat <<EOF
# DEXBTX miner — $TITLE

Pre-built binaries for BTX pool mining via \`dexbtx-miner\`. Designed to
drop onto any Linux + NVIDIA host without rebuilding from source.

## Artifacts

- **btx-gbt-solve** — solver with \`--share-target\` + \`--daemon\` patches.
  Native cubins for sm_61, sm_89, sm_90, sm_120; PTX for Turing/Ampere.
- **btx-daemon.tar.gz** — \`btxd\` + \`btx-cli\` for users running their
  own node alongside the miner (optional; install.sh does not require it).
- **btx-gbt-miner.py** — legacy solo-mining orchestrator (not used for pool).
- **SHA256SUMS** — integrity hashes for all of the above.

## What's patched on top of upstream

1. Pascal compute-capability bypass (lets sm_61 GPUs run the matmul kernel)
2. \`--share-target\` flag on the solver (early-exit when digest ≤ share target — required for pool mining)
3. \`--daemon\` mode (JSON-over-stdin/stdout protocol; persistent CUDA context across slices — eliminates per-invocation startup cost)

## GPU compatibility

| GPU class | Path |
|---|---|
| Pascal (GTX 9/10-series) | Native sm_61 cubin |
| Turing / Ampere (sm_70-sm_86) | sm_61 PTX → JIT to your arch |
| Ada Lovelace (RTX 40-series) | Native sm_89 cubin |
| Hopper (H100/H200) | Native sm_90 cubin |
| Blackwell (RTX 50-series) | Native sm_120 cubin |

## Verification

\`\`\`bash
sha256sum -c SHA256SUMS
\`\`\`

## Quick start

\`\`\`bash
curl -fsSL https://minebtx.com/install.sh | bash -s -- --address 'btx1z…'
\`\`\`

The installer downloads \`btx-gbt-solve\` from this release, SHA256-verifies
it, runs a hard GPU-acceleration smoke test, and writes a tuned config
based on your detected GPU. See the repo README for full docs.
EOF
)

# Create the release
echo ""
echo "=== creating GitHub release $TAG on $REPO ==="
gh release create "$TAG" \
    --repo "$REPO" \
    --title "$TITLE" \
    --notes "$NOTES" \
    btx-gbt-solve \
    btx-gbt-miner.py \
    btx-daemon.tar.gz \
    SHA256SUMS \
    README.md

echo ""
echo "=== release published ==="
gh release view "$TAG" --repo "$REPO"

echo ""
echo "=== bump install.sh + pyproject.toml in lockstep ==="
BASE="https://github.com/$REPO/releases/download/$TAG"
NEW_SHA=$(sha256sum btx-gbt-solve | awk '{print $1}')
echo "  install.sh:       PREBUILDS_TAG=\"$TAG\""
echo "                    EXPECTED_SHA256=\"$NEW_SHA\""
echo "  pyproject.toml:   expected_sha256 = \"$NEW_SHA\""
echo "                    prebuilds_release = \"$TAG\""
echo ""
echo "Solver binary URL (referenced by install.sh):"
echo "  $BASE/btx-gbt-solve"
