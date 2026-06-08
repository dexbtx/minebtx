# Changelog

All notable changes to `dexbtx-miner` are documented here. Format
follows [Keep a Changelog](https://keepachangelog.com/), versioning is
[Semantic Versioning](https://semver.org/).

## [0.3.5.1] — 2026-06-08 (fixup: GPU-enabled binary)

### Why
The initial 0.3.5 push shipped a `btx-gbt-solve` binary that was built without
CUDA experimental enabled — `--backend cuda` silently fell back to CPU on every
machine, collapsing hashrate ~100×. Fixed by rebuilding with
`-DBTX_ENABLE_CUDA_EXPERIMENTAL=ON -DBTX_CUDA_ARCHITECTURES="61;75;86;89;90;120"`
plus a one-line patch lowering `MIN_SUPPORTED_COMPUTE_CAPABILITY_MAJOR` from
upstream's `8` (Ampere+) to `6` — restores Pascal (sm_61, 10-series) and Turing
(sm_75, 20-series/T4) support that upstream dropped in v0.32.2.

### Verification before re-publishing
- C++ V2 seed derivation byte-matches Python reference (also matches the pool's
  Rust port by transitivity)
- GPU vs CPU digest equivalence on GTX 1070 (Pascal): identical digest at the
  same nonce on the V2 path (h=125001)
- New v0.32.2 GPU digest matches OLD v6.0 GPU digest at the same nonce on the
  legacy path (pre-125k)
- All 6 CUDA archs (sm_61, sm_75, sm_86, sm_89, sm_90, sm_120) embedded
- Live `--backend cuda` smoke draws 40W on the 1070 (idle baseline ~8W)

### Asset
- `btx-gbt-solve` Linux x86_64 — `sha256: 5a5938731dbb02337770d7dce34a576a8f90ca67919c295d72509a83c2c7ba8f`

### Action for existing miners
Re-run the installer; it's idempotent and preserves config:

```
curl -fsSL https://minebtx.com/install.sh | bash
```

## [0.3.5] — 2026-06-08 (MANDATORY: BTX v0.32.2 / matmul nonce-seed v2 at height 125,000)

### Why
BTX v0.32.1 / v0.32.2 introduces a consensus-mandated change at block 125,000:
the matmul A/B seeds are now derived per-nonce from the mutable header
(`DeterministicMatMulSeedV2`) instead of once per block. This is the
"E1 hardening" the chain devs called out as an explicit counter-measure
against cached-`A·B` precompute. Miners that don't re-derive seeds per
nonce will produce digests that the pool (and the chain) reject after
activation.

Activation block 125,000 — at the current network cadence, ~7h after
this release. **This is a mandatory upgrade for any miner that wants to
keep submitting shares past block 125,000.**

### Solver
- **btx-gbt-solve rebuilt against BTX v0.32.2 source.** Tag
  `btx-prebuilds-v0.32.2`, SHA256
  `86fd2f6de99cf735129fa1cb0f71078901bf22a34d21a682c547ab5eccd47a81`.
- Activates `nMatMulNonceSeedHeight = 125000` in the in-process consensus
  so `SolveMatMul` routes through `SolveMatMulNonceSeeded` post-fork.
  Each per-nonce attempt re-derives `seed_a` / `seed_b` from the
  (mutable) header before computing `A` and `B`.
- Restores `share_target_override` to v0.32.2's `pow.cpp` (the upstream
  release removed it). Preserves the block-derived `bnTarget` in a
  separate variable so pre-hash consensus gating still uses block-tier
  target while digest early-exit uses share-tier — same fix shipped in
  v0.3.0, ported forward.

### Installer
- `install.sh` bumps `PREBUILDS_TAG` → `btx-prebuilds-v0.32.2`,
  `EXPECTED_SHA256` → new binary hash.
- `pyproject.toml`: `tool.dexbtx-miner.solver.prebuilds_release` and
  `expected_sha256` follow.

### How to upgrade
Re-run the installer; it's idempotent and preserves config:

```
curl -fsSL https://minebtx.com/install.sh | bash
```

Or pinned URL (bypasses any cache):

```
curl -fsSL https://github.com/dexbtx/minebtx/raw/main/install.sh | bash
```

## [0.3.1] — 2026-05-28 (install.sh hotfix — non-mandatory for existing miners)

### Fixed
- **install.sh: `pip install dexbtx-miner` was looking up PyPI** (where this
  package is not published) and failing with `ERROR: No matching distribution
  found for dexbtx-miner` for any new miner running `install.sh` from scratch.
  Restored the v0.2.x GitHub-tarball install pattern, now pinned to the
  v0.3.1 release tag. Override via `DEXBTX_MINER_PKG_URL` for forks.
  Reported by a 5090 operator on first-install retry. Existing v0.3.0
  installations are unaffected (the Python package source is byte-identical
  between v0.3.0 and v0.3.1 — only `install.sh` changed).

## [0.3.0] — 2026-05-27 (MAJOR: solver v5.0 + stratum protocol v5)

This is the umbrella v5.0 release for the DEXBTX pool. Mandatory upgrade
— pre-v5 miners are rejected by the pool at the capability gate.

### Why
A consensus-level bug in the solver (Bug A) caused the early-exit
`pre_hash` filter to operate at share-tier difficulty instead of the
block-tier required by `btxd`'s `matmul phase2` PoW check. Every
candidate the pool submitted for ~24h was rejected ("matmul phase2
proof of work failed"), and the pool DB accrued ~234 BTX of phantom
credits via a now-removed recovery path on the maturation side. v5.0
ships the solver fix + a capability-based protocol gate so the pool
stops accepting work from pre-fix clients.

### Solver
- **btx-gbt-solve bumped from v4.4 to v5.0.0** (tag `btx-prebuilds-v5.0`,
  SHA256 `f750e55fee7ab1f7f7936487d1372f567e26f2df383a307589b1810f42c3247a`).
- Patch: `SolveMatMul` preserves the block-derived `bnTarget` in a
  separate variable BEFORE the `share_target_override` clobbers it,
  and passes that to both `BuildMatMulNonceBatchWindow` call sites as
  the pre_hash source. Result: the solver's pre_hash early-exit filter
  uses block-tier semantics matching `btxd`'s consensus check.
- Cubin coverage unchanged: sm_61 / sm_75 / sm_86 / sm_89 / sm_90 / sm_120.
- Validated: 25/25 mainnet block replay (heights 93k–113.3k, two
  independent populations) + regtest end-to-end (n=512, tight target,
  pre_hash on at h=0): one block mined + accepted + treasury paid.

### Stratum protocol (v5)
- **`mining.subscribe` extension**: a trailing dict now carries
  `protocol_compliant: ["pre_hash_block_tier_v18"]`, `hardware`
  (CPU/RAM/OS/GPUs), and `session_id`. Pre-v5 miners that don't send
  the dict (or omit `pre_hash_block_tier_v18`) get stratum error 401
  with an upgrade message; connection closed.
- **New `worker.report_metrics`**: sent every 60s with runtime
  telemetry (CPU util, RAM, per-GPU util/power/temp, solver nps,
  shares-session-total).
- **New `mining.set_canonical_name` server→client notification**:
  pool assigns a stable display name per physical GPU (format
  `{MODEL_NORMALIZED}-{NATO_PHONETIC}-{SEQUENCE}`, e.g. `5090-ALPHA-1`)
  keyed by `gpu_uuid`. Miner logs the assignment prominently and
  caches it in `~/.dexbtx-miner/canonical_names.json` so reconnects
  retain the info.

### Capability declaration (forward-compatible)
The gate checks for the capability *string*, not the client *identity*.
Third-party solvers (e.g. easybtx's Mac client) that ship an equivalent
fix can declare `pre_hash_block_tier_v18` and connect normally. The
pool also bans capability-liars: 3 `is_block` shares failing the
block-tier pre_hash check within 1h → 1h ban.

### New modules
- `dexbtx_miner.hardware` — CPU/RAM/OS/GPU enumeration via
  `/proc/cpuinfo`, `/proc/meminfo`, and `nvidia-smi`.
- `dexbtx_miner.canonical_names` — local cache of pool-assigned names.

### Install
`install.sh` now points at
`github.com/dexbtx/minebtx/releases/download/btx-prebuilds-v5.0/btx-gbt-solve`
(the old `github.com/btx-pool/btx-prebuilds` URL was a phantom — that
org never existed).

## [0.2.6] — 2026-05-27

### Fixed
- **Python 3.14 argparse failure**: literal `%` in the `--prepare-workers`
  help string ("sub-95%.") was unescaped. Python 3.14 tightened argparse's
  `%`-substitution handling, causing the miner to fail to start on 3.14
  with a string-formatting error. Earlier Pythons silently tolerated it.
  Now escaped as `%%`. Reported by a tester running v0.2.5 on Python 3.14.
- **install.sh smoke test false-negative on WSL2**: `install.sh` runs a
  CUDA engagement smoke test that asserts the solver appears in
  `nvidia-smi --query-compute-apps`. Under WSL2, nvidia-smi runs against
  the WDDM driver on the Windows host and can resolve the PID but
  cannot resolve the process name across the WSL2 namespace boundary —
  it returns `<PID>, [Not Found]`. The old assertion grep'd for the
  literal string `btx-gbt-solve` and failed on WSL2 even when the GPU
  was fully engaged at 100% util. Assertion (a) now also accepts any
  numeric-PID entry as evidence the GPU is running a compute kernel
  during the smoke window. The other two assertions (sustained power
  >100W, throughput floor >1000 N/s) continue to catch true CPU
  fallback. Reported by a tester running v0.2.5 on RTX 3060 Ti / WSL2.

### Confirmed in the wild
- First independent confirmation of v4.4 binary on RTX 30-series in
  the wild: 3060 Ti under WSL2 reported 100% GPU util at 217 W during
  steady-state mining. Retroactively validates the v0.2.5 republish.

## [0.2.5] — 2026-05-27 (REVERTS 0.2.4 — RESTORES v4.4 BINARY)

### Reverted
- **Reverts the v0.2.4 emergency rollback.** `PREBUILDS_TAG` is back to
  `v4.4-sm75-sm86`; `expected_sha256` is back to `ab70a6bc...` (v4.4's
  binary). v4.4-sm75-sm86 GitHub release re-created with the same binary.

### Why 0.2.4 was wrong
The v0.2.4 rollback was triggered by a controlled test on a GTX 1070
(Pascal sm_61) that observed 0 accepted shares in 7+ minutes with v4.4,
attributed to "100% code-23 rejection from wrong digests." Re-investigation
2026-05-27 proved that conclusion was a false positive:

- **v4.4's sm_61 matmul cubin** (SHA `fe7d947a...`) is **byte-identical**
  to v4.3's sm_61 matmul cubin.
- **v4.4's sm_61 oracle cubin** (SHA `1a28324b...`) is **byte-identical**
  to v4.3's sm_61 oracle cubin.
- **v4.4's host `.text` section** (1.7 MB at offset `0x1e380`) is
  **byte-identical** to v4.3's host `.text` section.
- The only structural difference between v4.3 and v4.4 is that v4.4 adds
  4 cubins (sm_75 matmul + oracle, sm_86 matmul + oracle) on top of v4.3.
  Everything sm_61-related is bit-identical.
- Direct re-test on the same hardware after re-installing v4.4: GTX 1070
  hit 100% GPU utilization at 133 W with v4.4, produced accepted shares
  with zero rejections.

The original test almost certainly observed a vardiff ramp confound — the
miner was within its first few minutes of a fresh session, at the pool's
default starting difficulty before vardiff had adapted, so the share
cadence looked nothing like the established v4.3 session it was compared
against. The mistaken codegen-regression conclusion led to an
unnecessary rollback that knowingly broke Ampere/Turing GPU mining for a
day.

### Side-effect of this revert
The CUDA 13 + Ampere/Turing silent-CPU-fallback gap (which v0.2.4
re-introduced as a known issue) is **fixed again** in v0.2.5 — RTX
20-series and 30-series GPUs go back to running natively on GPU under
CUDA 13.x drivers, no driver downgrade required.

### Migration
- Pure binary swap. `install.sh` re-run will fetch the v4.4 binary
  (`ab70a6bc...`). No config change needed.
- No protocol or stratum changes.

## [0.2.4] — 2026-05-27 (EMERGENCY ROLLBACK)

### Reverted
- **`PREBUILDS_TAG` reverted to `v4.3-sm89-native`** (was `v4.4-sm75-sm86`).
- `expected_sha256` reverted to `921c89fb...` (v4.3's binary).
- v4.4-sm75-sm86 release rescinded from GitHub.

### Why
The v4.4 binary shipped in v0.2.3 had a regression that broke matmul
digest computation on Pascal (sm_61). Symptoms: every share submission
rejected with code-23 (`digest >= share_target`) because the miner's
locally-computed digest didn't match what the pool re-computes. The
binary was structurally sound (all cubins present, daemon mode and
share-target flags intact) but produced incorrect digests on at least
sm_61. Anyone on a fresh install of v0.2.3 mining with a Pascal GPU
would have produced 100% rejected shares.

Reproduction (controlled test 2026-05-27):
- home-1070 with v4.4 binary: 0 accepted shares in 7+ minutes, 100%
  code-23 rejection
- home-1070 with v4.3 binary: 2 accepted shares in 7 minutes (normal
  rate for a Pascal 1070 at ~200-400 N/s)
- Same hardware, same config, same pool, same job — only difference
  was the binary

### Side-effect of the rollback
The CUDA 13 + Ampere/Turing silent-CPU-fallback issue from v0.2.2 and
earlier is BACK. Users on RTX 20-series / 30-series with CUDA 13 will
again see install.sh's smoke test fail. **This is being addressed in
v4.5** (currently under troubleshooting — the build pipeline that
produced v4.4 produces correct code for sm_89/sm_90/sm_120 but
something in the multi-arch combination broke sm_61 codegen).

### No protocol or config changes
Same stratum protocol, same per-session vardiff math, same payout
flow. Users who didn't reinstall during the v0.2.3 window were
already on v4.3 and were unaffected throughout.

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
