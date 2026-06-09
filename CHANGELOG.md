# Changelog

## [0.4.5] - 2026-06-09 (revert solver to BTX v0.32.2 - fix v0.32.3 GPU regression)

### Why
The v0.32.3 solver shipped in v0.4.0-0.4.4 rewrote the CUDA path (GPU-side SHA-256
seed derivation + GPU pre-hash scan). That rewrite regresses GPU mining post-block-
125000 - confirmed broken on Pascal sm_61 (daemon_ready then GPU wedges at 0%, zero
results) and the likely cause of fleet-wide saturation collapse. v0.32.2's solver
(matmul kernel byte-identical to the proven v0.30.1 kernel, CPU-side V2 seed
derivation, LTO) has no such regression (29.4 kN/s @ threads=4 on the same 1070
where v0.32.3 wedged).

### What
- All platforms revert to the btx-prebuilds-v0.32.2 solver release.
- x86_64 rebuilt to add sm_80 (arches 61;75;80;86;89;90;120), LTO on, bit-equivalent
  (CPU & CUDA) vs ci-refvec. sha bc605267.
- aarch64 (cuda12/cuda13), darwin-arm64 (Metal), rocm: existing v0.32.2 assets
  (aarch64 already includes sm_80).
- Node stays v0.32.3 (consensus-equivalent; no activation-height change). ZMQ and
  other node features are unaffected by the solver revert.

## [0.4.4] — 2026-06-09 (saturation tuning — 3.6× throughput on Pascal)

### Why
After v0.4.3 unblocked the wrapper, share rate was still well below
theoretical: on a GTX 1070 (Pascal sm_61) the solver did ~30 kN/s at
19% mean GPU util / 19W mean power. Lots of headroom unused. Time to
sweep configuration.

### What we found
Direct daemon-mode bench (bypasses wrapper IPC) at the "canonical
Pascal" config (`batch=32 prepare_workers=4 prefetch=4 solver_threads=4`)
sat at the same 30 kN/s. So wrapper IPC isn't the bottleneck — the
solver itself isn't saturating.

12-config sweep × 30s each, then a follow-up sweep on the top
candidate. Empirical findings on home-1070:

| Knob | Effect on kN/s | Effect on GPU util |
|---|---|---|
| `solver_threads`: 4 → 8 | 30 → 61 (2.0×) | 19% → 27% |
| `solver_threads`: 8 → 16 | 61 → 104 (1.7×) | 27% → 38% |
| `solver_threads`: 16 → 24 | 104 → 108 (1.05×) | 38% → 41% |
| `solver_threads`: 24 → 32 | regressed (CPU contention) | — |
| `batch_size`: 32 → 128 | ~flat | ~flat |
| `prepare_workers`: 4 → 16 | ~flat (without threads bump) | ~flat |
| `prefetch_depth`: 4 → 16 | ~flat | ~flat |
| `CUDA_POOL_SLOTS`, device-resident, noise-parallel | all neutral/regressive | — |

**The lever is `solver_threads`.** The "Pascal sweet spot" memory was
wrong about batch=32 / threads=4 — batch=128 / threads=24 is the
optimum on the 1070. Modern cards retain the same shape (more solver
parallelism feeds the kernel) so we ship one default profile.

### Verified end-to-end
5-min wrapper-driven run on home-1070 with the new config:
- Mean GPU util: **44%** (was 32%)
- Mean power: **82W** (was 19W)
- Peak power: **128W** (was 47W; 1070 TDP=150W → 85% TDP load)
- Solver CPU: **2400%** (24 cores fully saturated — system has 32, leaves
  headroom)
- 2 shares accepted in 300s (vs ~1/300s pre-v0.4.4)
- Effective ~124 kN/s on the 1070

### Why we stop at 44% GPU util
- VRAM stays at 642 MiB regardless of batch_size (32, 64, 128, 256, 512,
  1024) — kernel isn't VRAM-bound and bigger batches don't help
- Power at 128W of 150W TDP = 85% of TDP → close to fully driving the
  card; remaining gap is likely memory-bandwidth-bound on Pascal's
  256-bit GDDR5 (256 GB/s)
- Pascal sm_61 lacks tensor cores; the kernel does general FMA, not
  tensor-core-accelerated GEMM
- Going further requires kernel-level work (separate effort)

### What changed
- `install.sh` NVIDIA tuning: `solver_threads` now auto-scaled to
  `min(24, nproc-4)` (was hardcoded 4); `GPU_BATCH=128 GPU_PREFETCH=8
  GPU_WORKERS=16` for all NVIDIA cards (was a Pascal-special-case
  batch=32 / prefetch=4 / workers=4 that was actually suboptimal).
- No code change to the wrapper itself.

### Operator impact
- Existing operators running v0.4.1+: wrapper auto-update will fetch
  v0.4.4 on next restart, but the binary doesn't change — only the
  config defaults that install.sh writes on FIRST install. Existing
  `~/.dexbtx-miner/config.yaml` files keep their (suboptimal) values
  unless the operator re-runs install.sh with `--yes` to regenerate.
- For the FULL gain, operators should either re-run install.sh OR
  manually edit their `config.yaml` to set:
  ```
  solver_threads: 24            # or min(24, nproc-4) per their CPU
  solver_prepare_workers: 16
  solver_batch_size: 128
  solver_prefetch_depth: 8
  ```
- Pool-server v0.8.13's notifier already points at v0.4.x — no notifier
  bump needed (v0.4.4 falls under "you're behind, run install.sh").

## [0.4.3] — 2026-06-09 (CRITICAL: never drop stale-job-id slice results)

### The deeper bug
After v0.4.2 landed (carry nonce_start across clean=false notify), the
Mac tester ran v0.4.2 + v0.32.3 + pool-server v0.8.12 and got STILL
~zero shares despite the nonce_start carry-over working. CPU at 25%
(~2 cores) instead of the 700-900% expected on a saturated solver,
"23 notifies received, 0 shares produced."

### First-principles diagnosis
With pool emitting a notify every ~5s (mempool churn) and slices
taking 5-15s on a real workload, EVERY slice intersected with at
least one notify. The wrapper's `_solver_loop` (line 421) had a
stale-job-id check that DROPPED any slice result whose `job_id` no
longer matched the current `_current_job.job_id` — which meant every
slice's work was thrown away even when the slice's submitted nonce
WOULD have been accepted by the pool (the pool's JobCache, 8192 slots
since v0.8.6, keeps prior job_ids around precisely to serve same-parent
rotations).

Compounding: line 443 gated the nonce-progress write-back on the same
job_id check, so even unstale slices' nonce_end never updated
`_current_job.nonce_start`. Net: nonce stayed frozen across rotations
even when the v0.4.2 carry-forward was working as designed.

### Fix
Two semantic corrections in `stratum_client.py::_solver_loop`:

1. **Always submit found shares.** Pool's JobCache + share_validator
   handle staleness correctly. Wrapper has no business pre-filtering.
   When `_current_job.job_id != job.job_id`, log an info line
   acknowledging the rotation and submit anyway — pool will accept or
   reject as appropriate.

2. **Advance nonce_start across same-parent rotations.** Gate the
   `next_nonce_start` write-back on `previousblockhash` equality, not
   `job_id`. Same parent → our nonce-scan position is still valid
   against the new merkle root. Different parent (clean=True
   semantically) → leave the new job's broadcast `nonce64_start` in
   place; that's a true reset.

### Verified on home-1070 (GTX 1070 Pascal sm_61)
- 64 slices over 5 min observation window
- Nonce_start advancing continuously across 60+ clean=False notifies
- Solver finds + wrapper submits + pool ACCEPTS share — `share OK
  job=0x1f8 nonce=3564825495506 (a/r/b=1/0/0)`
- Pre-v0.4.3: 0 shares ever. Post-v0.4.3: shares flowing.

### Outstanding (separate work)
- GPU utilization ~32% (should be 99% on a well-tuned 1070) — slice
  cadence + per-slice setup overhead leaves the GPU idle most of the
  time. Pipeline overlap (next slice queued before current returns)
  would help. Separate effort.
- Share find rate is below theoretical given share_target math — also
  worth a separate kernel/protocol audit. Not blocking v0.4.3.

## [0.4.2] — 2026-06-09 (CRITICAL: carry nonce-progress on clean=false notify)

### The bug
On the M4 Mac fleet a tester reported throughput dropping to ~0 on the
v0.4.0 stack. Investigation showed the pool's stratum was emitting
`clean_jobs=true` on EVERY notify (since v0.3.39), including the ~5s
template-rebuild cadence from mempool churn — same parent block. Pool
side was fixed in pool-server v0.8.12. But the M4 tester saw notifies
*correctly* coming as `clean=false` post-fix and STILL the solver's
nonce_start was frozen across 6 consecutive notifies — never advancing
past 704374636544.

Root cause: `_on_notify` in `stratum_client.py` unconditionally did
`self._current_job = job` regardless of `clean`. The new job's
`matmul["nonce64_start"]` always came from the pool's broadcast, which
is fixed per-session (extranonce1 << 32 | base_offset). So every notify
reset the solver to the same starting nonce, even when the protocol
said "incremental update, keep cranking."

### Fix
On `clean=false` and there's a prior `_current_job`, carry forward
`prev._current_job.matmul["nonce64_start"]` into the new job before
swapping. On `clean=true` (real parent-block change) the broadcast
value stays as the reset point. One-line semantic correction; no
schema change, no protocol change.

### Operator impact
Anyone on v0.4.0 or v0.4.1 wrapper PAIRED with the v0.32.3 solver
binary will see throughput climb back to normal once they pick up
v0.4.2. Auto-update (from v0.4.1 onward) handles this automatically
on next miner restart. Operators still on ≤0.4.0 should re-run
install.sh once to bridge.

Pool-server v0.8.13 bumps `MinerUpdateNotifier`'s target to 0.4.2 so
TG-linked operators on <0.4.2 get a fresh DM.

## [0.4.1] — 2026-06-09 (wrapper auto-self-upgrade)

### Why
v0.3.6 shipped solver-binary auto-update; v0.4.0 bumped the solver to
BTX v0.32.3 and added BTX_CUDA_ALLOW_OLDER_GPUS=1 in the wrapper. But
the WRAPPER itself can only be upgraded by re-running install.sh or
`pip install --upgrade` — there's no automatic path from an older
wrapper to a newer one. Result: Pascal/Turing operators on v0.3.x
wrappers don't pick up the v0.4.0 env-var fix without operator action.

### What
- New `src/dexbtx_miner/wrapper_updater.py`. At process start the
  wrapper fetches `.solver-channel.json`, reads `version`, and if
  newer than `dexbtx_miner.__version__` pip-installs the new tag's
  tarball and `os.execvpe`s with the same argv. Mirrors
  solver_updater.py's defaults + fail-open semantics.
  - PEP-668 `--break-system-packages` retry like install.sh
  - Loop-guard via `DEXBTX_WRAPPER_JUST_UPGRADED` env var (broken
    release can't ping-pong the operator's miner)
  - Opt-out: `DEXBTX_NO_WRAPPER_AUTOUPDATE=1`
  - Overrides: `DEXBTX_MANIFEST_URL`, `DEXBTX_MINER_PKG_URL_TEMPLATE`
- `__main__.py::main()` calls `maybe_self_upgrade()` at the very top.
- `.solver-channel.json` version 0.4.0 → 0.4.1.
- `install.sh` DEXBTX_MINER_PKG_URL default → v0.4.1 tag.

### Operator impact
Operators on ≤0.4.0 still need ONE manual upgrade (re-run install.sh
or `pip install --upgrade dexbtx-miner`) to land on v0.4.1. From
v0.4.1 onward, wrapper updates flow automatically. Pool-server
v0.8.10 nudges TG-linked operators with a stale-version DM.

## [0.4.0] — 2026-06-09 (ship BTX v0.32.3 solver)

### Why
Upstream BTX v0.32.3 incorporated 3 of our 4 carried patches and shipped
a substantial CUDA matmul rewrite (release notes claim ~14k → ~2.45M
nonces/sec, ~174× faster). Time to migrate the fleet.

### What
- All 4 platform solver binaries rebuilt from `v0.32.3`:
  - `x86_64-linux` (CUDA 12.8, archs sm_61 through sm_120, LTO)
  - `aarch64-linux-cuda12` (sm_80;90;120)
  - `aarch64-linux-cuda13` (sm_80;90;120;121, GB10/Spark)
  - `arm64-darwin` (Metal)
- Patches dropped (now upstream):
  - 01-cuda-capability-gate (now env var `BTX_CUDA_ALLOW_OLDER_GPUS=1`)
  - 02-pow-h-share-target-override (in upstream pow.h)
  - 03-pow-cpp-share-target-override (in upstream pow.cpp)
- Only patch 05 (cmake build target for `btx-gbt-solve`) is still carried,
  plus the `btx-gbt-solve.cpp` drop-in itself.
- `gbt_solve_wrapper.py` sets `BTX_CUDA_ALLOW_OLDER_GPUS=1` by default via
  `env.setdefault` so Pascal/Turing GPUs keep working without manual env
  setup. No-op on Ampere+.

### Pascal / Turing notice
v0.32.3 defaults CUDA to sm_80+. Operators on 10xx, 16xx, or 20xx need
either re-run `install.sh`, `pip install --upgrade dexbtx-miner`, or
set `BTX_CUDA_ALLOW_OLDER_GPUS=1` manually. Otherwise CUDA silently
CPU-fallbacks ~100× slower.

All notable changes to `dexbtx-miner` are documented here. Format
follows [Keep a Changelog](https://keepachangelog.com/), versioning is
[Semantic Versioning](https://semver.org/).

## [0.3.7] — 2026-06-08 (per-platform binary dispatch in solver auto-update)

### Why
v0.3.6 shipped solver auto-update with a single-binary manifest schema
that implicitly assumed Linux x86_64. With aarch64 (Grace, GB10/Spark)
and Mac (Apple Silicon) binaries now published alongside the x86_64 one,
miners on those platforms need the auto-updater to pick the right
binary, not the Linux x86_64 one.

### How

`.solver-channel.json` extended from `{sha256, url}` to a `{platforms:
{...}}` dict keyed by `{arch}-{system}` (e.g. `x86_64-linux`,
`aarch64-linux`, `arm64-darwin`). The wrapper detects the host's
platform via `platform.machine() + sys.platform`, looks up the matching
entry, and downloads + verifies the right binary for THIS host.

The legacy v1 schema (top-level sha256+url) is still recognized —
treated as x86_64-linux only. So manifests published before v0.3.7
still work for x86_64-linux miners, but won't auto-update aarch64 or
darwin miners (the v2 schema is required for those).

### Platform keys recognized

| Key | Detected when |
|---|---|
| `x86_64-linux` | `platform.machine() in (x86_64, amd64)` and Linux |
| `aarch64-linux` | `platform.machine() in (aarch64, arm64)` and Linux |
| `arm64-darwin` | `platform.machine() == arm64` and macOS |
| `x86_64-darwin` | `platform.machine() == x86_64` and macOS (Intel Mac) |

Operator override: `DEXBTX_PLATFORM_KEY=<any-key>` env var forces a
specific manifest entry. Useful for cuda12-vs-cuda13 disambiguation on
aarch64-linux (default picks cuda13; set `DEXBTX_PLATFORM_KEY=aarch64-linux-cuda12`
to fetch the cuda12 build).

### What changed

- `src/dexbtx_miner/solver_updater.py`: added `detect_platform_key()` +
  `_resolve_manifest_entry()`; refactored `maybe_update_solver` to use them.
- `.solver-channel.json`: rewrote to v2 schema with 4 platform entries
  (x86_64-linux, aarch64-linux, aarch64-linux-cuda12, arm64-darwin).
- `__init__.py` + `pyproject.toml`: version 0.3.6 → 0.3.7.

### Status of each platform binary in this release

- x86_64-linux: production, validated end-to-end on the home-1070
- aarch64-linux (cuda13/sm_121, GB10): published, **not yet hardware-validated**
- aarch64-linux-cuda12 (sm_80;90;120): published, **not yet hardware-validated**
- arm64-darwin: published, **EXPERIMENTAL Mac Metal test build, not validated**

If you're on Mac or aarch64 and want to test, you're now auto-served the
right binary on next miner restart. If something goes wrong (binary
fails to run, etc.), the wrapper keeps a `.pre-autoupdate-bak` of the
previous binary at the same path with that suffix.

## [0.3.6] — 2026-06-08 (self-updating installer + solver auto-update)

### Why
Two classes of "stale on miners' disks" problems we keep running into:

1. **Stale `install.sh`** — operators have an older install.sh saved or
   are hitting a cached CDN copy. It pins to an older release tag whose
   binary fails the share-target smoke check. Two operators hit this
   today.
2. **Stale solver binary** — a long-running rig keeps the same binary
   forever even when we ship a new one. Forks require chasing operators
   down via Telegram.

This release future-proofs both, going forward.

### How

- **install.sh self-update bootstrap.** Before doing anything else, it
  re-fetches itself from `github.com/dexbtx/minebtx/raw/main/install.sh`,
  compares the embedded `INSTALL_SH_VERSION`, and re-execs the newer
  copy if outdated. Opt-out: `DEXBTX_NO_SELFUPDATE=1`. Fails open if
  GitHub raw is unreachable.

- **Solver auto-update via manifest.** On every miner startup, the
  Python wrapper fetches `.solver-channel.json` from the repo root,
  compares the local binary's SHA256 against the manifest's, and
  atomically replaces it (with backup) if outdated. Opt-out:
  `DEXBTX_NO_SOLVER_AUTOUPDATE=1`. Same fail-open behavior — a network
  blip never blocks mining.

- **Fork-mandatory upgrade lever.** The manifest supports a
  `min_required_sha256` field. When set, miners whose local SHA differs
  AND can't successfully upgrade refuse to mine (raising
  `SolverUpdateRequired`). This is the lever for forks: publish the
  binary, set `min_required_sha256`, and every miner's next process
  spawn either upgrades or refuses — no Telegram chase required.

### What changed

- New file: `src/dexbtx_miner/solver_updater.py` (manifest fetch +
  SHA-pinned download + atomic install + opt-out + hard-floor enforcement).
- Wired into `src/dexbtx_miner/__main__.py` startup before
  `StratumClient.run_forever()`.
- `install.sh` gains a ~25-line bootstrap block at the top.
- New file: `.solver-channel.json` at repo root, points at the current
  v0.3.5.2-era binary (no functional change — same binary, just now
  reachable via the manifest mechanism).
- `__init__.py` version → 0.3.6, `pyproject.toml` version → 0.3.6.

### Action for operators

Re-run install.sh once via the raw-GitHub URL to pick up the bootstrap:

```
curl -fsSL https://github.com/dexbtx/minebtx/raw/main/install.sh | bash
```

From then on, both the installer and the solver binary self-update on
every run. No further manual intervention required at future fork events.

## [0.3.5.2] — 2026-06-08 (fixup: LTO build flag)

### Why
The 0.3.5.1 binary was built without link-time optimization. The previous
v6.0 (v0.30.1-base) build had LTO enabled (504 `.lto_priv.*` symbols in the
shipped binary); we accidentally lost it on the v0.32.2 migration. Without
LTO the CPU-side dispatch + input-prep path is 3–5× slower, which starves
the GPU and shows up as low power draw / low share rate.

Fixed by rebuilding with `CMAKE_INTERPROCEDURAL_OPTIMIZATION=ON`. Verified
empirically on the GTX 1070: try-rate per slice went from ~100k/s → ~278k/s
(~3× improvement). Binary size dropped 14.7 MB → 5.75 MB (LTO collapsed
duplicate code).

### Asset
- `btx-gbt-solve` Linux x86_64 — `sha256: b9251a06133abb90a71d714c3a83ea9accb71ba81352b6226ca50c7e5fae5032`

### Action for existing miners
Re-run the installer; it's idempotent and preserves your config:

```
curl -fsSL https://minebtx.com/install.sh | bash
```

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
