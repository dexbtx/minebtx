# Tuning `dexbtx-miner` for your GPU

The miner's throughput is dominated by **six solver environment
variables** passed to `btx-gbt-solve`. The defaults in `install.sh` are
empirically validated across Pascal through Blackwell — the universal
profile is `workers=16 / threads=8 / batch=128 / prefetch=8`.

If your GPU sustains <95% util at the default, the two levers that
actually move the needle are `solver_prepare_workers` and
`solver_threads`. **Batch size has zero meaningful effect on
steady-state throughput** once prep is sized correctly (we verified
this with sweeps over batch=32, 64, 128, 256, 512 on 4060 Ti and 5070).

---

## The six levers

| Env var | What | Reasonable range | Universal default |
|---|---|---|---|
| `BTX_MATMUL_BACKEND` | Backend selection | `cuda` (NVIDIA), `metal` (Apple), `cpu` | `cuda` |
| `BTX_MATMUL_GPU_INPUTS` | CPU vs GPU generates matmul inputs | `0` or `1` | **`0` (mandatory)** — CPU-gen is the "GPU saturation breakthrough"; without this every modern card caps at 8% util / 33W |
| `BTX_MATMUL_SOLVE_BATCH_SIZE` | Nonces per kernel launch | 16 → 512 | **`128`** universally. Tested 64–512 on multiple cards; no measurable difference once `workers` is right. Avoid 256+ on 5070 Ti (broke CUDA historically). |
| `BTX_MATMUL_PREPARE_PREFETCH_DEPTH` | Queue depth for matmul input prep | 4 → 16 | **`8`** is the universal sweet spot |
| `BTX_MATMUL_PREPARE_WORKERS` | CPU threads for input gen | 8 → 16 | **`16`** universally (4060 Ti and 5060 Ti are fine at 12; 16 doesn't hurt). **KEY LEVER** — bump alongside `SOLVER_THREADS` if util is sub-95% |
| `BTX_MATMUL_PIPELINE_ASYNC` | `1` overlaps prep + kernel launch | `0` or `1` | **`1`** always (unless debugging) |

Plus `BTX_MATMUL_SOLVER_THREADS`, the parallel-solve worker count:
**universal default `8`** (raised from the historical `4`).
**KEY LEVER** — `PREPARE_WORKERS` and `SOLVER_THREADS` are the two
levers that actually move the needle; they go hand in hand. If util is
sub-95% sustained, bump both together (typically 8→16 and 4→8).

---

## Per-GPU measured profiles

Every entry below was measured on real hardware via a parameter sweep
during pool onboarding. The "Power %" column is sustained draw as a
percentage of the card's spec TDP — anything near 100% means the
kernel is fully saturating the GPU's power-frequency curve.

| GPU | Arch | workers | threads | batch | Util | Power / TDP |
|---|---|---|---|---|---|---|
| **GTX 1070** (8GB) | Pascal sm_61 | 16 | 8 | 128 | 83% | 113 / 150 W (75%) — memory-bound, Pascal's ceiling |
| **RTX 2080 Ti** (11GB) | Turing sm_75 | 12 | 8 | 128 | **100%** | 296 / 250 W (boost-enabled host) |
| **RTX 3060 Ti** (8GB) | Ampere sm_86 | 12 | 8 | 128 | **100%** | 190 / 200 W (95%) |
| **RTX 4060 Ti** (16GB) | Ada Lovelace sm_89 | 12 | 8 | 128 | **100%** | 164 / 165 W (99%) |
| **RTX 5060 Ti** (16GB) | Blackwell sm_120 | 16 | 8 | 128 | 99% | 150 W (canonical reference) |
| **RTX 5070** (12GB) | Blackwell sm_120 | 16 | 8 | 128 | **99.9%** | 223 / 250 W (89%) |
| **RTX 5070 Ti** (16GB) | Blackwell sm_120 | 16 | 8 | 128 ⚠️ | TBD | Avoid `batch=256` — historically broke CUDA on this card |

Cards not in the table (RTX 30-series consumer, 40-series consumer
beyond 4060 Ti, 5080, 5090, H100, etc.): start at the **universal
default** `workers=16 / threads=8 / batch=128 / prefetch=8` and run the
benchmark. The installer auto-writes this profile for any unknown GPU.

### Why Pascal caps at 83%

The GTX 1070's matmul kernel is memory-bandwidth bound regardless of
CPU input-prep rate. You can throw `workers=16/threads=8` at it but
the on-GPU memory subsystem becomes the bottleneck before the kernel
can ingest more input. 113W / 150W TDP at 83% util is the Pascal
ceiling — same hashrate as the old `workers=4/threads=4/batch=32`
profile, just standardized for config consistency. **Don't waste time
sweeping** — Pascal is what Pascal is.

### Why 5060 Ti gets 99% at workers=8 but 5070 needs 16

The 5070 has more SMs and a higher boost clock than the 5060 Ti — it
consumes input faster, so the prep pipeline starves at workers=8.
Bumping to workers=16 takes the 5070 from 70.6% util → 99.9% util. The
5060 Ti can hit 99% at workers=8, but **16 also works fine on the
5060 Ti** and standardizing simplifies install.sh and docs.

---

## Architecture coverage in the shipped binary

The `btx-gbt-solve` binary released with `dexbtx-miner` embeds native
CUDA cubins for these architectures, plus PTX so the driver can JIT to
any compatible newer arch on first launch (~5-10% slower than native
on first launch, then steady):

| GPU class | Compute capability | Path |
|---|---|---|
| Pascal (GTX 10/9-series) | sm_61 | **Native sm_61 cubin** |
| Volta (V100) | sm_70 | sm_61 PTX → JIT |
| Turing (RTX 20-series, GTX 16-series) | sm_75 | sm_61 PTX → JIT (proven on 2080 Ti) |
| Ampere A100 | sm_80 | sm_61 PTX → JIT |
| Ampere consumer (RTX 30-series) | sm_86 | sm_61 PTX → JIT (proven on 3060 Ti) |
| Ada Lovelace (RTX 40-series) | sm_89 | **Native sm_89 cubin** |
| Hopper (H100, H200) | sm_90 | **Native sm_90 cubin** |
| Blackwell (RTX 50-series) | sm_120 | **Native sm_120 cubin** |

If your GPU isn't covered (e.g. a very new arch not yet in the
binary), `install.sh`'s Phase 0.3b smoke test will fail loudly during
install rather than silently mining on CPU. File an issue at
github.com/dexbtx/minebtx/issues with your `nvidia-smi -q` output
and we'll ship a build that supports your hardware.

---

## Names that DO NOT exist (don't waste your time)

These look right but the solver source doesn't read them — they're
silent no-ops:

- ❌ `BTX_MATMUL_PREFETCH` (wrong, use `BTX_MATMUL_PREPARE_PREFETCH_DEPTH`)
- ❌ `BTX_MATMUL_BATCH_SIZE` (wrong, use `BTX_MATMUL_SOLVE_BATCH_SIZE`)
- ❌ `BTX_SOLVE_BATCH_SIZE` (wrong, prefix is always `BTX_MATMUL_`)

The authoritative list — paste this to confirm any variable name is
real before relying on it:

```bash
strings ~/.dexbtx-miner/bin/btx-gbt-solve | grep -oE 'BTX_MATMUL_[A-Z_]+' | sort -u
```

---

## Run the built-in benchmark

```bash
dexbtx-miner benchmark
```

That sweeps batch sizes `32, 64, 128, 256` for 30 seconds each and
reports real **nonces/sec** for each (not the misleading `tries_used`
counter — see below). Pick the winning batch + apply it.

Examples:

```bash
# Quick scan (default — 4 batches × 30s = ~2 min total):
dexbtx-miner benchmark

# Wider sweep on a beefy card:
dexbtx-miner benchmark --batches 32,64,128,256 --prefetches 4,8,16 --workers 4,8,16
# (33 configs × 30s ≈ 16 min)

# Longer per-config window (more statistical confidence):
dexbtx-miner benchmark --duration 60

# Write the winner straight to config:
dexbtx-miner benchmark --write-config
```

The benchmark uses a deterministic test job (a real historical BTX
job) — it doesn't talk to the pool, doesn't submit shares, doesn't
cost anything. Safe to re-run any time.

---

## How to verify your tuning is actually working

After the miner has been running >2 minutes:

```bash
# 1. Solver daemon alive (single long-running process — daemon mode)
pgrep -af "btx-gbt-solve.*--daemon"
# Should return exactly one PID. If it churns (different PID every check),
# the daemon protocol is broken; the wrapper is spawning per slice.

# 2. Compute-app visible — proves CUDA actually engaged
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader
# If empty when GPU shows util, you have silent CPU fallback.

# 3. Util at target
nvidia-smi --query-gpu=utilization.gpu,power.draw --format=csv,noheader
# Match against the table above for your card.

# 4. Env vars correctly reaching subprocess
cat /proc/$(pgrep -f 'btx-gbt-solve.*--daemon')/environ | tr "\0" "\n" | grep BTX_MATMUL
# Should show BACKEND=cuda, GPU_INPUTS=0, SOLVE_BATCH_SIZE=128,
# PREPARE_WORKERS=16, PREPARE_PREFETCH_DEPTH=8, PIPELINE_ASYNC=1,
# SOLVER_THREADS=8 (or your custom config).
```

---

## Why the solver's `tries_used` counter is misleading (and `nonce64` too)

Neither field in the solver's JSON output is "nonces tested per second":

- `tries_used` is an internal counter that doesn't map 1:1 to nonces
  evaluated. We've seen it under-count real work by orders of
  magnitude.
- `nonce64` is the **position** of the winning nonce when one is
  found, NOT the count of nonces tested. Two different cards finding
  the same deterministic share will both report the same `nonce64`
  value — only `elapsed_s` differs.

**For absolute N/s, the only honest measure is btxprice.com's
network-derived metric** (`N_eff = total_work / time` over a chain
window). For a single miner that's `(your block-find rate / network
block-find rate) × network_N_eff`.

**For relative config tuning on the same hardware**,
`dexbtx-miner benchmark` reports time-to-find a deterministic share.
Faster config = faster solver for your card.

---

## CPU fallback troubleshooting

If `nvidia-smi` shows the GPU at idle clocks / power during mining,
the solver silently fell back to CPU. Symptoms:

- Throughput 100× lower than expected
- GPU temperature stays cold (<50°C)
- No process appears in `nvidia-smi --query-compute-apps`

Common causes:

1. **Driver too old.** `btx-gbt-solve` v4.x requires NVIDIA driver ≥
   565 for the dual-arch sm_61 + sm_89/90/120 native cubin path.
   Check: `nvidia-smi --query-gpu=driver_version --format=csv,noheader`.
2. **`BTX_MATMUL_BACKEND` not set or set to `cpu`.** Verify with the
   /proc/environ check above.
3. **CUDA runtime not findable.** `btx-gbt-solve` static-links
   libcudart but still needs the NVIDIA driver libs (`libcuda.so`).
   If those are in a non-standard location, set `LD_LIBRARY_PATH` to
   include them.
4. **Binary lacks compatible kernel image** for your arch — won't
   happen on Pascal through Blackwell (we have cubins/PTX for all),
   but theoretically possible on cards we don't cover. **Don't
   downgrade your driver** — file an issue and we'll ship a build.
5. **GPU OOM.** Default `batch=128` needs ~700 MiB VRAM. Cards with
   <8GB free shouldn't trip this; if you do, drop to 64.

---

## Recap: the tuning loop

1. **Trust the installer's auto-detected profile first.** It's empirically
   validated for every architecture from Pascal through Blackwell.
2. **Check util after 2 minutes** with `nvidia-smi`. If ≥95% sustained,
   you're done.
3. **If util < 95% sustained**: bump `solver_prepare_workers` to 16
   (and `solver_threads` to 8) in your config. Restart miner.
4. **If you have an odd card not in the per-GPU table**: run
   `dexbtx-miner benchmark` for a head-to-head against a few batch
   sizes + worker counts. Apply the winner.
5. **Re-run benchmark** when you upgrade your driver, change cards, or
   want to push harder. Cards behave differently as the network's
   epsilon_bits parameter shifts over chain history.

Share what works in the Telegram chat ([@btxdexbot](https://t.me/btxdexbot))
— empirical data from real miners is the best way for everyone to find
their sweet spot faster.
