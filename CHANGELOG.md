# Changelog

All notable changes to `dexbtx-miner` are documented here. Format
follows [Keep a Changelog](https://keepachangelog.com/), versioning is
[Semantic Versioning](https://semver.org/).

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
