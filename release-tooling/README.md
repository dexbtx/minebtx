# Release tooling

Helpers for publishing new versions of the `btx-gbt-solve` solver
binary (and the legacy daemon `btxd` + `btx-cli` if you ship those
alongside) as GitHub Release assets on this repo.

Most users never touch this directory — the `install.sh` at the
repo root downloads from the latest release automatically. This
folder is only relevant if you're cutting a new release.

## Files

| File | Purpose |
|---|---|
| `SHA256SUMS` | Pinned hashes for the binaries in the current release tag. The repo root's `install.sh` and `pyproject.toml` reference the `btx-gbt-solve` SHA from here. |
| `publish-release.sh` | `gh release create` helper. Bundles the binaries, verifies SHAs, attaches to a new release tag. |

## How a release is cut

1. **Build the solver binary** from upstream BTX source with the
   `--share-target` + `--daemon` patches applied. Use **portable
   build flags** so the binary runs on any modern Linux/CUDA host:

   ```bash
   # On a build host with CUDA 12.8+ toolkit:
   cmake -B build-prod -S . \
       -DBTX_ENABLE_CUDA_EXPERIMENTAL=ON \
       -DBTX_CUDA_ARCHITECTURES="61;89;90;120" \
       -DCMAKE_CUDA_ARCHITECTURES="61-real;89-real;90-real;120-real" \
       -DCMAKE_BUILD_TYPE=Release \
       -DCMAKE_C_FLAGS_RELEASE="-O3 -march=x86-64-v3 -flto=auto -fno-plt" \
       -DCMAKE_CXX_FLAGS_RELEASE="-O3 -march=x86-64-v3 -flto=auto -fno-plt" \
       -DCMAKE_EXE_LINKER_FLAGS_RELEASE="-flto=auto -static-libstdc++ -static-libgcc"
   cmake --build build-prod --target btx-gbt-solve -j$(nproc)
   ```

   The build flags above matter — see "Portability requirements" below.

2. **Verify the binary actually contains CUDA device code:**

   ```bash
   /usr/local/cuda-12.8/bin/cuobjdump --list-elf build-prod/bin/btx-gbt-solve
   # Expected: at least sm_61, sm_89, sm_90, sm_120 cubins listed.
   # If "does not contain device code" → -DBTX_ENABLE_CUDA_EXPERIMENTAL=ON
   # was missing from the cmake invocation. Fix and rebuild.
   ```

3. **Sanity-check `--daemon` and `--share-target` flags are present:**

   ```bash
   build-prod/bin/btx-gbt-solve --help | grep -E "daemon|share-target"
   ```

4. **Update `SHA256SUMS`** in this directory with the new hash.

5. **Publish via `publish-release.sh`:**

   ```bash
   ./publish-release.sh dexbtx/minebtx v4.4-yourdescriptor
   ```

6. **Bump the pinned hash + tag** in the repo root's `install.sh`
   (`EXPECTED_SHA256` + `PREBUILDS_TAG`) and `pyproject.toml`
   (`[tool.dexbtx-miner.solver]`). Commit + push.

## Portability requirements (don't skip)

These three settings are MANDATORY for the binary to run on the wide
range of Linux/CUDA hosts that miners use:

| Setting | Why |
|---|---|
| `-DCMAKE_BUILD_TYPE=Release` | Without this, cmake silently ignores `CMAKE_*_FLAGS_RELEASE`. Symptom: huge binary with dynamic libstdc++ even though you "set" the static flag. |
| `-static-libstdc++ -static-libgcc` (linker flags) | Build hosts with gcc-13+ produce references to `GLIBCXX_3.4.32+`. Stock Ubuntu 22.04 ships gcc-11 = max `GLIBCXX_3.4.30`. Dynamic-linked binary fails at startup with `version GLIBCXX_3.4.32 not found`. |
| `-march=x86-64-v3` (C/CXX flags) | Default `-march=native` produces a binary that hits "Illegal instruction" on older CPUs. `x86-64-v3` is the Haswell+ baseline (AVX2) — covers practically every modern miner host. |

## Architecture coverage in the shipped binary

The `BTX_CUDA_ARCHITECTURES="61;89;90;120"` flag bakes in native
cubins for:

- `sm_61` — Pascal (GTX 10-series)
- `sm_89` — Ada Lovelace (RTX 40-series)
- `sm_90` — Hopper (H100, etc.)
- `sm_120` — Blackwell (RTX 50-series)

Intermediate architectures (Turing sm_75, Ampere sm_80/sm_86) run via
the embedded sm_61 PTX → JIT to their respective arch on first kernel
launch (~5-10% slower than native on first invocation, then steady).

## Versioning

Use semantic-ish tags like `vN.N-descriptor`:

| Tag pattern | Example | Meaning |
|---|---|---|
| `vN.N-arch-yyy` | `v4.3-sm89-native` | Major release with a new arch added |
| `vN.N.N-fix-yyy` | `v4.0.1-cuda-flag-fix` | Hotfix for a critical issue |
| `vN.N-perf-yyy` | `v4.4-perf-prefetch` | Performance work, same ABI |

Include in the GitHub release notes:

- Patches applied vs upstream (e.g. Pascal compute-capability bypass,
  `--share-target` early-exit, `--daemon` stdin/stdout protocol)
- CUDA toolkit version used to build
- Source commit hash from the BTX repo this was built from
- Architecture compatibility list (cuobjdump --list-elf output)
- SHA256 of each released artifact

## Build-from-source fallback

If you don't trust prebuilt binaries (smart instinct), build from
source on your target host with the flags above. ~10-20 minutes per
machine vs ~2 minutes for a prebuilt download.

The patches that turn upstream BTX `btx-gbt-solve` into the pool-ready
variant touch three files:

- `src/pow.h` — adds one optional `share_target_override` parameter
- `src/pow.cpp` — ~10 lines for the early-exit check
- `src/btx-gbt-solve.cpp` — ~20 lines for the `--share-target` parser,
  plus `--daemon` mode (read JSON jobs from stdin, run `RunOneJob` per
  iteration, write JSON result line to stdout, persist CUDA context
  across iterations — eliminates per-slice subprocess startup cost)

The patch set is in PRs against the upstream BTX repo (linked from the
release notes of each tag). Or grep this repo's `gbt_solve_wrapper.py`
for the protocol the daemon expects on stdin/stdout.
