# Changelog

All notable changes to `dexbtx-miner` are documented here. Format
follows [Keep a Changelog](https://keepachangelog.com/), versioning is
[Semantic Versioning](https://semver.org/).

## [0.2.3] — 2026-05-27

### Added — solver binary `btx-gbt-solve` v4.4 with native Turing + Ampere cubins
- **Native `sm_75` cubin** for Turing (RTX 20-series, T4)
- **Native `sm_86` cubin** for Ampere consumer (RTX 30-series — 3060/3060 Ti/3070/3080/3090)

The shipped solver now embeds 6 native cubins (sm_61, sm_75, sm_86,
sm_89, sm_90, sm_120) — no PTX-JIT fallback required for any
dominant consumer GPU.

### Fixed
- **CUDA 13 + Turing/Ampere silent CPU fallback** ([dexbtx#issues](https://github.com/dexbtx/minebtx/issues)).
  v0.2.2 and earlier shipped only 4 native cubins (sm_61/sm_89/sm_90/
  sm_120) and relied on the sm_61 PTX section to JIT-compile to
  sm_75/sm_86 at driver load. CUDA 13 deprecated compute_61 as a JIT
  source, breaking the path for every 20-series and 30-series miner
  on a current driver. v0.2.3 fixes this by including the missing
  arches as native cubins — no driver downgrade, no JIT dependency.

### Release artifact details
- Solver SHA256: `ab70a6bc6a3756c5adbc85d7eba90bca370b39b4b6acb8610b45cca994771b98`
- `PREBUILDS_TAG`: `v4.4-sm75-sm86`
- Binary size: 6.6 MB (up from 5.6 MB in v4.3 — two extra cubins)
- Built with CUDA 12.8 toolkit, gcc x86-64-v3, LTO, static libstdc++

### Migration
- New installs: `install.sh` automatically fetches v4.4 binary.
- Existing installs: re-run `install.sh` to pull the new binary. No
  config change needed. Pool-side protocol is unchanged.
- Users on CUDA 12.x: no functional change — the same `--daemon` +
  `--share-target` solver flags work, share rate identical.
- Users on CUDA 13 with a 20- or 30-series GPU: previously broken,
  now works at full GPU utilization.

## [0.2.2] — 2026-05-27

### Added
- **CUDA 13 + Ampere/Turing compatibility note in README.** Documents
  the silent CPU-fallback failure mode that surfaces when a 30-series
  / 20-series GPU runs against a CUDA 13.x driver. Root cause is
  NVIDIA's progressive deprecation of compute_61 PTX as a JIT source;
  the shipped solver binary's sm_61 PTX no longer JIT-compiles to
  sm_75 / sm_86 on a current driver. README now spells out the
  symptom, the diagnosis (check `nvidia-smi`'s CUDA version), the
  workarounds (wait for binary rebuild or self-build with
  `CMAKE_CUDA_ARCHITECTURES=86`), and **explicitly tells users not to
  downgrade their driver**.
- "Confirmed working on" column to the Architecture Coverage table so
  users can tell at a glance which paths still work on which CUDA
  toolkit versions.

### Notes
- The fix for the underlying gap — native `sm_75` and `sm_86` cubins
  in `btx-gbt-solve` — lives in BTX upstream's build matrix. Upstream
  issue filed; rebuilt binary will ship in the next release once the
  upstream build pipeline produces it (or sooner if we self-rebuild).
- Pool-side `nonce64_start` personalization (per-session nonce-range
  scoping) deployed to production on 2026-05-26. **No miner-side
  change required** — the pool now sends each session its own unique
  `nonce64_start` in `mining.notify` params[8].nonce64_start. Solvers
  that respect that value (dexbtx-miner does, line
  `stratum_client.py:310`) automatically sweep disjoint nonce ranges,
  eliminating duplicate-share collisions in same-machine multi-instance
  setups. Pre-fix, dual-instance miners saw up to 70%+ rejection rates
  from `UNIQUE(job_id, nonce)` collisions; post-fix, same setups run
  at 95-99% accept rates.

## [0.2.1] — 2026-05-25

### Added
- Hardened the stratum client per external 16-finding code review (15
  of 16 findings addressed; H2 deferred). Notable items: stricter
  notify validation (fail loud on missing matmul fields rather than
  silently mining at height 0 with placeholder seeds), per-job nonce
  replay protection, vardiff-aware share submission, share-target
  hex passed through to the patched `btx-gbt-solve` solver for
  share-tier early-exit.
- `USER_AGENT` constant derived from `__version__` so the value
  reported to the pool stays in sync with the package version.

### Fixed
- User-Agent string no longer hardcoded to `dexbtx-miner/0.1.0`
  ([e954066](https://github.com/dexbtx/minebtx/commit/e954066)).

## [0.2.0] — 2026-05-24

### Added
- Initial public release of `dexbtx-miner`.
- Async daemon-mode wrapper around `btx-gbt-solve` (solver
  `v4.3-sm89-native`) — keeps the CUDA context + cubins loaded across
  slices, eliminates the ~5s per-slice context init cost.
- Stratum 2.0-matmul client with vardiff-aware share submission.
- `install.sh` for one-line setup on Linux + WSL2: provisions config,
  downloads the patched solver binary, verifies SHA256, runs a GPU
  smoke test.
