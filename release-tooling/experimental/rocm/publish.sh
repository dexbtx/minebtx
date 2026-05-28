#!/bin/bash
# Publish the experimental ROCm release. Run AFTER:
#   1. PAT is fresh (regenerate at https://github.com/settings/tokens)
#   2. `gh auth login --with-token < <(echo $GH_TOKEN)` succeeded
#   3. btx-gbt-solve binary is present in this directory
set -e

cd "$(dirname "$0")"
TAG=btx-prebuilds-v5.0-experimental-rocm

if [ ! -f btx-gbt-solve ]; then
    echo "FATAL: btx-gbt-solve missing in $(pwd)"
    echo "Get the rebuilt binary from /mnt/c/Users/atti4/BTX/tmp/btx-gbt-solve-rocm-FOR-UPLOAD"
    exit 1
fi

# Verify the binary matches what's in SHA256SUMS — guards against an
# accidental swap of the pre-Bug-A-fix archive for the post-fix build.
echo "=== verifying SHA256SUMS ==="
sha256sum -c SHA256SUMS

# Tag is already pushed via git. This just attaches the binary + README
# as a GitHub release.
echo "=== creating release ==="
gh release create "$TAG" \
    btx-gbt-solve \
    --repo dexbtx/minebtx \
    --title "Experimental ROCm/HIP build (btx-gbt-solve only)" \
    --notes-file README.md
echo "=== done. Release URL above. Share with AMD testers."
