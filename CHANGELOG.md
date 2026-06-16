# Changelog

## [0.4.19-darwin-hotfix] - 2026-06-16 (arm64-darwin solver only; manifest hotfix — NO wrapper change)

### What
- `.solver-channel.json` `arm64-darwin` entry: `sha256` → `ee83d3fc846c7ecefdd3490a8f30b3f1294e274299d1b534b4c6245f709a40ba`, url unchanged (re-hosted at `btx-prebuilds-v0.32.11-preempt/btx-gbt-solve-darwin-arm64`, clobbered). The asset is now a **real btx v0.32.11 source build** (e1e5673 + `05-cmakelists-add-gbt-solve-target.patch` + the v0.4.17 `btx-gbt-solve.cpp` preempt main), replacing the v0.32.10-carried binary.
- Top-level `version` **unchanged at 0.4.19**, all other platform entries unchanged, `solver_updater.py`/`wrapper_updater.py` unchanged. This is a solver-binary hotfix delivered purely through the per-platform `sha256`; the wrapper does not move (no pip churn).
- Phase 1: `min_required_sha256` left at `361abdad` (soft — download failure falls back to the old valid binary, never refuse-to-mine). Phase 2 (after fleet uptake): raise `min_required_sha256` → `ee83d3fc`.

### Why
The shipped `arm64-darwin` solver was the v0.32.10-carried binary, whose MatMul-V3 GPU pre-hash scan is half-wired → CPU per-nonce fallback at/after mainnet block 130,500. Measured on Apple M4: shipped binary **0.15M nonce-scan/s** vs a v0.32.11-source build **5.0M nonce-scan/s** → **~33×**. Every fleet Mac on the auto-updater was mining at ~1/33 of capable hashrate. (Live confirmation: pool dashboard — eBTX Macs ~80 nps; our M4 on the source build was top Mac on the board.) The v0.32.11 *source* has the V3 scan wired; the Metal inline-source metallib fallback carries the scan kernel, so it engages regardless of metallib packaging.

### Validation
- KAT: `cpu_digest == metal_digest == reference` on the post-125000 V2 vector (`ci-refvec.json`, h130000).
- V3 bit-exact: 64/64 distinct nonces `cpu == metal` at h131000 + parent_mtp.
- Live soak: APPLEM4-GOLF-1 on minebtx.com, 52 min, **345+ accepted / 0 rejected / 0 code-23**, difficulty converged 131072, GPU scan engaged the whole time.

### CI gap (follow-up)
`build-solver-macos-arm64.yml` previously gated only on the digest KAT — it never exercised scan throughput, so a CPU-fallback binary passed green (how the v0.32.10-carried asset shipped). A scan-rate assertion (`> 1M nonce/s` at a hard target) is staged for the macOS lane and will land separately (needs a token with `workflow` scope).

## [0.4.19] - 2026-06-16 (periodic wrapper re-check; SOLVER BINARY UNCHANGED — additive)

### What
- The background re-check loop (`_solver_update_watcher`, v0.4.16) now re-checks the **wrapper version each cycle** (`maybe_self_upgrade`), not just the solver binary. No-op when current; pip-upgrade + re-exec on a newer publish. Version lockstep → `0.4.19` (`__init__.py`, `pyproject.toml`, `.solver-channel.json` `version`, `install.sh`); solver entries untouched.
- Carries the v0.4.18 solver auto-heal unchanged.

### Why
v0.4.18 (the first solver-*identical*, wrapper-only release) exposed a gap: the v0.4.16 periodic re-check only re-checked the **solver** binary and re-exec'd on a SHA change. The wrapper self-upgrade (`maybe_self_upgrade`) ran **only at process startup** — on running rigs it propagated *only* by riding along on a solver re-exec. With an unchanged solver, no re-exec fired, so v0.4.18 could not reach a long-running rig without a manual restart. v0.4.19 makes wrapper-only updates self-propagate from here on.

### Note
This fixes propagation for releases **from v0.4.19 onward** — a rig must be on 0.4.19+ for the periodic wrapper check to run (chicken-and-egg). 0.4.17/0.4.18 rigs still pick up 0.4.19 on their next restart; thereafter wrapper-only updates need no restart.

## [0.4.18] - 2026-06-16 (wrapper solver auto-heal; SOLVER BINARY UNCHANGED — additive to v0.4.17)

### What
- Wrapper-only release. The solver binary is **identical to v0.4.17** (`70f16afd` / `btx-prebuilds-v0.32.11-preempt`); the channel's per-platform solver URLs/SHAs/`min_required_sha256` are **untouched**. Only the wrapper version + pip tarball move (`__init__.py`, `pyproject.toml`, `.solver-channel.json` top-level `version`, `install.sh` pkg URL → `v0.4.18`).
- New `stratum_client._heal_loop` watchdog + `GbtSolveWrapper.force_restart()`: bounces the long-running solver daemon when it wedges, on either trigger (both work-proportional, so ramp-ups / slow GPUs / high vardiff don't false-trip):
  - **B (wrong-digest):** `≥ heal_consec_rejects` (default 8) consecutive submitted-and-rejected shares with zero accepts since the last accept. Resets on any accept.
  - **A (hang):** no solver result for `heal_solver_stall_secs` (default 90 s) while the process is alive (the case where `a/r/b` freezes and B can never fire).
- Escalating cooldown prevents restart loops; resets the instant an accept lands. Fresh window on every (re)connect. All thresholds in `config.heal_*`.

### Why
The long-running solver daemon can latch a bad runtime state around a pool restart's job discontinuity — emitting gross-wrong MatMul-V3 digests (100% code-23) or hanging — and a pool reconnect does **not** clear it (the daemon persists across stratum reconnects). Affected rigs mine **0 valid shares until the process restarts**, which no pool-side action (reconnect, socket kick) can do remotely. Diagnosed 2026-06-16: stuck vs healthy rigs ran the **same** wrapper + solver binary, version- and batch-independent; the jobs were provably valid (other miners accepted the same job_ids). A full miner restart revived a stuck rig where pool reconnects + a pool-side socket kick did not.

### Validation
- home-1070 (GTX 1070): after a pool restart the solver daemon wedged (a/r/b frozen at 111/0; pool rejects climbed 0→8, 0 accepts). Trigger B fired at 8 rejects, `force_restart` bounced the daemon (respawn ~6 s), first accept ~10 s later; single heal, no loop, rejects frozen at 8 thereafter. The wrapper self-healed the exact failure with no human intervention.
- Healthy rig: watchdog armed, **0 false fires** over normal mining.

### Rollout
0.4.16+ wrappers self-upgrade to 0.4.18 via `wrapper_updater` (channel `version`) on their next re-check — no manual restart, no solver re-download (solver SHA unchanged). Fresh installs pull v0.4.18 via `install.sh`.

## [0.4.15] - 2026-06-14 (all NVIDIA + Spark platforms → btx-prebuilds-v0.32.11; MatMul-V3 GPU-scan fix)

### What
- `.solver-channel.json` `x86_64-linux` entry: sha → `3f7bd3f7e92d07377459a14416fca1b8460e224540ba179bc3309e36af326853`, url → `btx-prebuilds-v0.32.11/btx-gbt-solve`. CUDA 12.8, native cubins `sm_61;sm_75;sm_80;sm_86;sm_89;sm_90;sm_120` (GTX 10-series → RTX 50-series) + LTO. Built on home-1070. `min_required_sha256` set to the same so hosts on the v0.32.10 binary force-upgrade.
- `.solver-channel.json` `aarch64-linux` + `aarch64-linux-cuda12` entries: sha → `ca911dc06c79900a89afccee778b437fd353b0b78c57df231c3bc3007b590196`, url → `btx-prebuilds-v0.32.11/btx-gbt-solve-aarch64-linux-gnu-cuda12`. CUDA 12.8, archs `sm_80;sm_90;sm_120`. Built via `build-solver-aarch64.yml`.
- `.solver-channel.json` `aarch64-linux-cuda13` entry: sha → `8337f2fd0335849b5aa3b9841f03c548596affe0be9f0260691dc14959e312e2`, url → `btx-prebuilds-v0.32.11/btx-gbt-solve-aarch64-linux-gnu-cuda13`. CUDA 13.0, archs `sm_80;sm_90;sm_120;sm_121` (Grace + GB10/Spark). Built via `build-solver-aarch64.yml`.
- `arm64-darwin` entry unchanged (stays on `btx-prebuilds-v0.32.10`; Metal rebuild deferred — outside this release's NVIDIA+Spark scope).
- `pyproject.toml`, `src/dexbtx_miner/__init__.py`, `install.sh` bumped to `0.4.15` / `btx-prebuilds-v0.32.11` / the new SHAs in lockstep.
- All Linux binaries built from `btxchain/btx` tag `v0.32.11` (commit `601b2cc`) + patch `05-cmakelists-add-gbt-solve-target.patch` + `btx-gbt-solve.cpp`.

### Why
v0.32.10 (the v0.4.14 fork release) shipped the MatMul-V3 GPU pre-hash scan half-wired: at/after mainnet block 130,500 GPU miners fell back to the slow CPU per-nonce path (util ~1-7%, hashrate cratered). Shares stayed consensus-VALID — a throughput regression, not a correctness bug. Upstream v0.32.11 is the official fix (V3 seed builder added to the CUDA + Metal scan kernels, `IsMatMulParentMtpSeedActive` gates removed, `SetDeterministicMatMulSeeds` fails closed, + a CUDA CI parity lane asserting scan flags == `CheckMatMulPreHashGate`).

### Validation
- home-1070 (GTX 1070 / sm_61), 10 min via the real wrapper: 99% GPU util with the scan engaging (vs 1-7% on v0.32.10), 54 shares / 0 rejects / 0 stale, code23=0.
- x86_64 fat binary: `cuobjdump` confirms all 7 cubins embedded; functional re-run 99% util / 0 rejects.
- aarch64 cuda12 + cuda13: CI bit-equivalence CPU parity test passed (consensus-correct); cubins verified structurally.

### Rollout
All NVIDIA + Spark hosts auto-upgrade to the v0.32.11 solver on next miner restart (solver_updater `min_required` gate); the wrapper auto-updates to 0.4.15 via `wrapper_updater`.

## [0.4.13] - 2026-06-13 (all platforms → btx-prebuilds-v0.32.8)

### What
- `.solver-channel.json` `x86_64-linux` entry: sha → `50ec2e4ecd685c7e0e1199746405ee0bd94bc70a40f63bd1fa7989992a89b799`, url → `btx-prebuilds-v0.32.8/btx-gbt-solve`. CUDA 12.8, archs `sm_61;sm_75;sm_80;sm_86;sm_89;sm_90;sm_120`. LTO enabled. Built on home-1070, KAT-verified bit-equivalent (first-share digest `ca43457e54ba7e7d5cd6dfbbf17583887ae78cb75391df4e2a0c1d941ff413d6` at nonce=1) to the v0.32.5 production binary on the loose-target reference job.
- `.solver-channel.json` `aarch64-linux` entry: sha → `e4f03f4f91f019bfcc3d82291cfbcbd7e9f89cf309820f2d51a6c06cccab6be9`, url → `btx-prebuilds-v0.32.8/btx-gbt-solve-aarch64-linux-gnu-cuda13`. CUDA 13.0, archs `sm_80;sm_90;sm_120;sm_121` (Grace + Blackwell, including GB10/Spark).
- `.solver-channel.json` `aarch64-linux-cuda12` entry: sha → `a738d4e8543a9a3224c5c94059d5668ade96dcbdcd76f118619eb8370a79104d`, url → `btx-prebuilds-v0.32.8/btx-gbt-solve-aarch64-linux-gnu-cuda12`. CUDA 12.8, archs `sm_80;sm_90;sm_120`.
- `arm64-darwin` entry unchanged (still on `btx-prebuilds-v0.32.2` pending an Apple Silicon rebuild from the v0.32.8 source tree). Defer to a future point release.
- All three aarch64/x86_64 binaries built from `btxchain/btx` tag `v0.32.8` applying patch `05-cmakelists-add-gbt-solve-target.patch` (the PR58 oracle-accel + matmul-accel patches 10/11 are now upstreamed in v0.32.8 source — only the cmakelists target patch remains).

### Why
v0.32.8 ships consensus-stable hardening: recovery-exit fee replacement, recovery-exit mempool liveness, mining health RPC, local deep-reorg protection profile, plus CUDA matmul solver pipeline optimizations (CPU-side nonce-seed pipeline — reduces repeated CPU setup work). Consensus rules unchanged from v0.32.5 (block-125000 V2 nonce-bound seed).

Field-tested on home-1070 (GTX 1070 / Pascal sm_61) for 51 min: 203 shares accepted, 0 rejected, 0% reject rate, throughput essentially at parity with v0.32.5 basis (settled-window average ~1438 N/s vs ~1500 baseline = within 4%, inside vardiff-noise margin). **Pascal is GPU-bottlenecked, so the CPU-pipeline opt has no measurable headroom to give back on this rig.** CPU-bound rigs (5090 / 5070 Ti on shared-CPU hosts, Grace+Blackwell) are where the optimization should land measurable gains; verification by those operators encouraged.

### Wrapper code
No changes from v0.4.11. The local v0.4.12 session_gen-cursor experiment was tested and **did not deliver** — post-reconnect rejects cascaded to 47% over 5 min and 95% over 10 min on home-1070, matching v0.4.11's pre-fix behavior. v0.4.12 was never published; v0.4.13 carries v0.4.11 wrapper code unchanged. Investigation into the cross-session-cursor / post-reconnect reject pattern continues separately.

### Effect on operators
- **x86_64-linux:** solver auto-update will pull the new `btx-prebuilds-v0.32.8/btx-gbt-solve` binary on next miner restart.
- **aarch64-linux + aarch64-linux-cuda12:** solver auto-update will pull the new aarch64 binaries from `btx-prebuilds-v0.32.8`.
- **arm64-darwin (Apple Silicon):** no change; remains on `btx-prebuilds-v0.32.2`.
- **No config changes required.**
- **No mining-path code changes** — same wrapper-solver protocol, same C4 multi-share batching, same telemetry.


## [0.4.11] - 2026-06-11 (aarch64-linux + aarch64-linux-cuda12 → btx-prebuilds-v0.32.5)

### What
- `.solver-channel.json` aarch64-linux entry: sha → `462551244c5a8a05bde294e2102baaecbbf77f5f9ee31aad76e6e9a34ade3a10`, url → btx-prebuilds-v0.32.5/btx-gbt-solve-aarch64-linux-gnu-cuda13. CUDA 13.0, archs sm_80;sm_90;sm_120;sm_121 (Grace + Blackwell, including GB10/Spark).
- `.solver-channel.json` aarch64-linux-cuda12 entry: sha → `409aa27aecea8ca389b4afd0b625f866b457a8aef82255ccac87bd53bcb62c57`, url → btx-prebuilds-v0.32.5/btx-gbt-solve-aarch64-linux-gnu-cuda12. CUDA 12.8, archs sm_80;sm_90;sm_120.
- arm64-darwin entry unchanged (still on btx-prebuilds-v0.32.2 pending Apple Silicon rebuild).
- Both aarch64 binaries built via `.github/workflows/build-solver-aarch64.yml` on GitHub's `ubuntu-24.04-arm` runner from `btxchain/btx` tag `v0.32.5`, applying patches `05-cmakelists-add-gbt-solve-target.patch` + `10-pr58-oracle-accel-windowed-sha.patch` + `11-pr58-matmul-accel-factored-compression.patch`. cmake flags match the x86_64 ship build (Release + LTO + the relevant CUDA_ARCHITECTURES for each variant).
- KAT-equivalent to the x86_64 build **by construction** (identical source tree, identical patches, identical compiler flags except architecture targets). Not GPU-tested on an aarch64 host because the GitHub runner has no GPU; first aarch64 operator that picks this up via solver_updater will be the first runtime validation.

### Why
The v0.32.5 channel notes said "aarch64 builds pending." Closing the loop so Grace + Graviton + GB10/Spark operators get the same PR58+C4 throughput win that x86_64 got at v0.4.7. Manifest-only update; wrapper code is byte-identical to v0.4.10.

## [0.4.10] - 2026-06-11 (Telemetry sampling no longer stalls the solver it's measuring)

### Why
v0.4.8/0.4.9's multi-sample GPU stats fixed single-snapshot's miss-the-burst
problem but introduced a subtler one: `collect_runtime_metrics()` ran inline
on the asyncio event loop. The ~2s sample window blocked the loop, which
stalled wrapper reads of the solver subprocess's stdout pipe. The solver
filled its stdout buffer trying to send share results, blocked on the
write, paused its scan kernel — and the GPU went idle EXACTLY while the
wrapper was sampling it. Result: the wrapper accurately measured the stall
it was causing (~2% util / ~44W), the dashboard's recommendation engine
tagged the rig "Below saturation," and operators saw "host bottleneck"
on a rig that was actually running at 98% / ~130W between metric ticks.

Caught on home-1070 — live nvidia-smi showed 98% / 113W, the wrapper
reported 2% / 44W, the two could not both be right. Direct standalone
calls to `hardware._gpu_runtime()` outside the wrapper returned realistic
averages (62-98%), confirming the function itself was fine and the
problem was the calling context.

### What
`StratumClient._metrics_loop()` now runs `collect_runtime_metrics()` in
the default ThreadPoolExecutor via `loop.run_in_executor(None, ...)`. The
event loop keeps draining solver I/O during the ~2s sample window, the
solver never pauses, and the wrapper measures the actual mining state
instead of an artifact of its own sampling.

Verified on home-1070 canary: first post-fix metric tick read
util=98 / power=109.9W matching live nvidia-smi, dashboard recommendation
flipped from "Below saturation" to "Optimal."

### Effect on operators
- Dashboard's util/power averages now reflect reality even during the
  sample tick.
- No change to mining/share-submission path, solver, or any user-visible
  config. Same 60s metric cadence.

## [0.4.9] - 2026-06-11 (Defense-in-depth: wrapper auto-update loop-guard)

### Why
v0.4.7 shipped with `__init__.py.__version__ = "0.4.6"` — the constant was never
bumped alongside pyproject.toml or `.solver-channel.json`. The wrapper auto-updater
reads `__init__.py.__version__` to decide whether to self-upgrade. After
pip-installing the v0.4.7 tarball it would re-exec, see __version__ still as
"0.4.6" (because the new tarball ALSO had "0.4.6"), and re-trigger the upgrade
forever. v0.4.8 fixed the immediate bumper mistake; v0.4.9 makes the auto-updater
**immune to the bug class itself** so a future packager mistake can't ever
infinite-loop the fleet again.

### What
`wrapper_updater.maybe_self_upgrade()` now enforces "at most one upgrade attempt
per process exec." The loop-guard previously only fired when the new package's
`__version__` >= target (the successful-upgrade case). It now also fires when
`__version__` < target after a fresh upgrade — that's the signature of a
malformed release (channel.json/pyproject.toml bumped without bumping
`__init__.py.__version__`). The branch:

- Logs a WARNING naming both the target version pip-installed and the actual
  `__version__` so the operator can report it.
- Returns without retrying — mining continues on whatever code is now installed.

If the new tarball is somehow worse than what we had, the operator can still
opt out with `DEXBTX_NO_WRAPPER_AUTOUPDATE=1` or pin
`DEXBTX_MINER_PKG_URL_TEMPLATE` to an older release.

### Effect on operators
- v0.4.9 carries forward all of v0.4.8's telemetry fixes (multi-sample GPU stats,
  populated cohort fields).
- Any future "bumper forgot to bump `__init__.py`" mistake will surface as a
  single warning line per restart instead of an infinite pip-install loop.
- Mining / share-submission path bit-for-bit unchanged from v0.4.7.

## [0.4.8] - 2026-06-11 (Accurate telemetry + v0.4.7 upgrade-loop hotfix)

### Why (telemetry)
The dashboard was showing newly-upgraded v0.4.7 / v0.32.5 rigs as "Below saturation
— likely host or config bottleneck" even though they were running at 98% GPU util /
~135W. Two bugs in the wrapper's `worker.report_metrics`:

1. **Single-snapshot GPU sampling.** `_gpu_runtime()` called `nvidia-smi --query-gpu`
   once per metrics tick. Post-fork mining is bursty (scan kernel runs ~3 s, then
   the host iterates flags briefly). A single instantaneous sample lands in the
   inter-scan gap ~80% of the time on Pascal, underreporting util to 2 % and power
   to the idle floor (~42 W). The dashboard then averaged ten of these bad samples
   over 10 min and triggered the "host bottleneck" recommendation.
2. **H2/H3 cohort fields not populated.** The pool's f55c7f6 migration added
   `wrapper_version` / `solver_sha256` / `solver_backend` to `worker_metrics` for
   the canary-vs-stable A/B cohort view, but the wrapper wasn't sending them, so
   `/api/fleet` couldn't tell which workers had upgraded.

### What
- `hardware._gpu_runtime()` now multi-samples nvidia-smi 4x over a ~2 s window and
  averages `util_pct` + `power_w` (temp_c is taken from the latest sample). Catches
  at least one in-kernel sample on any realistic post-fork mining cadence. Single-
  GPU adds ~2 s to each 60 s metrics tick; multi-GPU is the same because all rows
  return from one nvidia-smi call.
- `hardware.collect_runtime_metrics()` now accepts `wrapper_version`,
  `solver_sha256`, and `solver_backend` keyword args and includes them in the
  payload (older pool servers ignore the extra fields gracefully).
- `hardware.solver_sha256_hex(path)` helper — reads the installed solver binary
  and returns its sha256. Called once at `StratumClient.__init__`.
- `StratumClient` caches the three cohort fields at init (computed once per
  process — solver sha256 only changes when the auto-updater swaps the binary,
  which forces a restart) and passes them on every `report_metrics`.

### Why (v0.4.7 upgrade loop hotfix)
The v0.4.7 release shipped with `__init__.py.__version__ = "0.4.6"` — the constant
was never bumped alongside pyproject.toml or .solver-channel.json. The wrapper
auto-updater reads `__init__.py.__version__` to decide whether to upgrade. After
`pip install`ing the v0.4.7 tarball it would re-exec, see __version__ still as
"0.4.6" (because the tarball also had "0.4.6"), and re-trigger the upgrade —
infinite loop. The loop-protection guard in `wrapper_updater.maybe_self_upgrade()`
only catches loops where the NEW package's __version__ >= target; with the version
stuck below target on every tarball, the guard never fires.

This v0.4.8 release bumps `__init__.py.__version__` to "0.4.8" (in lockstep with
pyproject.toml and channel.json) AND adds a comment in `__init__.py` warning
future bumpers about the three-place lockstep. Stuck v0.4.7 operators automatically
escape on their next iteration cycle (they upgrade to v0.4.8, see matching
__version__, comparison succeeds, mining starts normally).

### What
- `hardware._gpu_runtime()` now multi-samples nvidia-smi 4x over a ~2 s window and
  averages `util_pct` + `power_w` (temp_c is taken from the latest sample). Catches
  at least one in-kernel sample on any realistic post-fork mining cadence. Single-
  GPU adds ~2 s to each 60 s metrics tick; multi-GPU is the same because all rows
  return from one nvidia-smi call.
- `hardware.collect_runtime_metrics()` now accepts `wrapper_version`,
  `solver_sha256`, and `solver_backend` keyword args and includes them in the
  payload (older pool servers ignore the extra fields gracefully).
- `hardware.solver_sha256_hex(path)` helper — reads the installed solver binary
  and returns its sha256. Called once at `StratumClient.__init__`.
- `StratumClient` caches the three cohort fields at init (computed once per
  process — solver sha256 only changes when the auto-updater swaps the binary,
  which forces a restart) and passes them on every `report_metrics`.
- `__init__.py.__version__` bumped to "0.4.8" + comment block warning future
  bumpers about the three-place lockstep (pyproject.toml, __init__.py,
  .solver-channel.json).

### Effect on dashboard
- Rigs running v0.4.8 will report realistic util/power averages within the first
  metrics cycle (60 s after restart).
- `/api/fleet` cohort grouping starts populating for v0.4.8 rigs, enabling
  canary-vs-stable A/B views by exact solver build.
- Bit-for-bit unchanged on the mining / share-submission path. Telemetry-only.

## [0.4.7] - 2026-06-11 (GPU saturation: PR#58 kernel opts + C4 continuous feeding)

### Why
Post-fork (v0.32) the per-nonce matmul seeds force a full n^3 digest recompute per
share, and the GPU-nonce-seed scan path disables the parallel solver, so NVIDIA rigs
(all on WSL) ran at 5-15% of card power -- bursty feeding idles the clock to P8/~9W.
Two stacked causes: (1) slow / low-power-density CUDA kernels; (2) the solver
early-exiting on the FIRST share each slice, starving the GPU between bursts.

### What
- Kernel layer (PR #58, byte-exact; verified locally on Pascal sm_61 = 5.4x, author
  on 5090 = ~3x): windowed SHA-256 in scanner + matrix-gen, per-template/per-seed SHA
  midstates, single-block digest reduction, and factored compression (n^3 -> ~n^2
  MACs). Added as build-patches 10/11 (src/cuda/oracle_accel.cu, matmul_accel.cu),
  applied in the CUDA (aarch64) and ROCm builds. Verified byte-identical nonce /
  matmul_digest / full-C vs the unpatched binary on a post-125000 V2 job.
- C4 layer (continuous GPU feeding):
  - Solver build-patch (btx-gbt-solve.cpp): RunOneJob now LOOPS SolveMatMul,
    collecting ALL shares in the slice (new shares[] + share_count; first share still
    mirrored at top level for back-compat) instead of returning to the host on the
    first hit -- removes the per-share stdin round-trip so the GPU stays fed.
  - Wrapper (stratum_client.py): submit every share from shares[], firing each
    _submit_share as a background asyncio task so a slice-worth of submits never
    starves the daemon.
- Measured on home-1070 (GTX 1070, WSL): 9W/P8 -> P2, 120-126W peaks, ~40% util
  (98% spikes), 0 rejects, 0 tracebacks. Remaining host-side bounce to full 99%
  saturation is tracked as C2 (GPU-side scan compaction).
- Source / build-patch only. The x86_64-linux release binary and the
  .solver-channel.json / install.sh tag bumps are a SEPARATE, gated release step --
  no fleet auto-update is triggered by this commit.

## [0.4.6] - 2026-06-09 (disable solver header-time-refresh - fix code-23 rejects)

### Why
Post-block-125000 the matmul seed is derived from the full header including nTime.
btx-gbt-solve's SolveMatMul auto-refreshes the header nTime to wall-clock after
BTX_MINER_HEADER_TIME_REFRESH_ATTEMPTS (default 4096) attempts, but the result only
reports nonce+digest, never the refreshed time. The wrapper then submits the original
job ntime, so the pool recomputes the digest against a DIFFERENT time and rejects the
share code-23 (digest >> share_target). Post-fork the ~10x per-nonce slowdown means
rigs routinely exceed 4096 attempts before finding a share, so the refresh fires
constantly -> heavy code-23, worst on CPU-starved/low-thread rigs.

### What
- Wrapper sets BTX_MINER_HEADER_TIME_REFRESH_ATTEMPTS=4294967295 (effectively disabled)
  via env.setdefault, so the solver mines the exact job header and its digest matches
  what the pool validates.
- Solver binary UNCHANGED (still BTX v0.32.2). Wrapper-only; flows via auto-update.
- Pool-side companion fix (validate digest against the submitted ntime, matching btxd)
  tracked separately.

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
