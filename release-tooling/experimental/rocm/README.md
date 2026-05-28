# EXPERIMENTAL — AMD ROCm/HIP build of `btx-gbt-solve`

> **Status:** Built but **never run on the live pool**. You are the first.
> **Support level:** Best-effort, community-driven. Report issues + findings to **@shibuyayume** on Telegram.

This is a separate release track from the canonical NVIDIA/CUDA builds. It exists so AMD GPU owners can try DEXBTX pool mining. The kernel logic is the same as the CUDA build (the HIP source is `src/hip/*.hip` compiled into the `CUDA` runtime slot via macro shims), but it's never been validated on the live pool with real share submissions.

## What's in this folder

| File | What it is |
|---|---|
| `btx-gbt-solve` | HIP/ROCm solver binary, ~12.5 MB |
| `SHA256SUMS` | Integrity hash |

## Supported AMD architectures

This binary contains device code for:

- **`gfx1030`** — RDNA2 consumer (RX 6800, 6800 XT, 6900 XT, 6950 XT)
- **`gfx1100`** — RDNA3 consumer (RX 7900 XT, 7900 XTX)

Other AMD architectures (older RDNA1, CDNA datacenter parts, integrated APUs) are not compiled in. If you want a build for a different arch, contact @shibuyayume.

## Requirements

- Linux x86_64 (no macOS, no Windows native — try WSL2 or a Linux VM)
- ROCm 6.x installed (`/opt/rocm` or similar). The binary dynamically links `libamdhip64.so.6`.
- `BTX_MATMUL_BACKEND=cuda` at runtime — yes, **cuda**, not `hip`. The HIP build piggybacks on the same backend slot.

## Build provenance

- Source: `dexbtx/btx` HEAD as of 2026-05-28, post-Bug-A pre-hash fix in `src/pow.cpp`
- Compiler: `hipcc` (ROCm's clang++)
- Build flags: `-DBTX_ENABLE_HIP_EXPERIMENTAL=ON -DBTX_HIP_ARCHITECTURES="gfx1030;gfx1100"`
- Binary SHA256: `11af6c702ac60513a880d0e870915622ab9c2b1ec79a379af8b1162885f935bd`

**Earlier May 27 build was withdrawn** — it was built ~7 hours before the Bug A pre-hash fix landed in source. If you have a copy of `btx-gbt-solve-v4-5-rocm-experimental` dated before 2026-05-28, **discard it** — your `is_block` candidates would be silently rejected by the pool.

## Quick-start test protocol

Five steps, ~10 minutes total. Don't skip steps — if anything fails, stop and report instead of patching around it.

### 1. Verify the binary loads + sees your GPU

```bash
LD_LIBRARY_PATH=/opt/rocm/lib ./btx-gbt-solve --help
```

Should print help text starting with `Usage: btx-gbt-solve [flags]`. If you see `libamdhip64.so.6: cannot open shared object file`, your ROCm install is not in the dynamic linker path. Either:

- Set `LD_LIBRARY_PATH=/opt/rocm/lib` (as above), or
- Run `sudo ldconfig /opt/rocm/lib` once

### 2. Standalone CPU smoke test

```bash
LD_LIBRARY_PATH=/opt/rocm/lib BTX_MATMUL_BACKEND=cpu ./btx-gbt-solve \
    --version 4 --prev-hash 0000000000000000000000000000000000000000000000000000000000000000 \
    --merkle-root 0000000000000000000000000000000000000000000000000000000000000000 \
    --time 1700000000 --bits 1e02a876 \
    --seed-a 0000000000000000000000000000000000000000000000000000000000000000 \
    --seed-b 0000000000000000000000000000000000000000000000000000000000000000 \
    --block-height 1 --max-tries 100
```

Expect: it runs, returns a nonce (or "no solution found"), exits cleanly. If this fails, the binary is broken — stop and report.

### 3. GPU smoke test

```bash
LD_LIBRARY_PATH=/opt/rocm/lib BTX_MATMUL_BACKEND=cuda ./btx-gbt-solve \
    [same flags as above]
```

Expect: same behavior, much faster. If it falls back to CPU silently or errors with "no GPU found", post your `rocminfo` output to @shibuyayume.

### 4. Inspect dynamic deps

```bash
ldd ./btx-gbt-solve | grep -E 'hip|rocm|amd'
```

Expect: `libamdhip64.so.6 => /opt/rocm/lib/libamdhip64.so.6` (or wherever ROCm is). If `not found`, fix the ldd path before going further.

### 5. Pool mining smoke (5 minutes)

Install **`dexbtx-miner` v0.3.2+** (older versions don't send solver env vars,
so the pool can't give you tuning feedback):

```bash
curl -sSL https://minebtx.com/install.sh | bash
```

That drops `dexbtx-miner` + a default config + the **CUDA** `btx-gbt-solve`.
Replace the CUDA binary with this one:

```bash
# Find where install.sh dropped the solver
which btx-gbt-solve     # or: find ~ -name btx-gbt-solve

# Replace it (path will vary)
cp ./btx-gbt-solve /path/to/installed/btx-gbt-solve
chmod +x /path/to/installed/btx-gbt-solve
```

Or set `gbt_solve_path` explicitly in your `~/.dexbtx-miner/config.yaml`:

```yaml
gbt_solve_path: /full/path/to/our/rocm/btx-gbt-solve
pool_host: minebtx.com
pool_port: 3333
worker: <your-btx1z-address>.amd-test
```

Then run `dexbtx-miner` and watch logs:

- ✅ Shares get accepted → working
- ❌ Many "share rejected: pre_hash" or "matmul phase2 proof of work failed" → binary bug, stop and report
- ⚠️ Connects but submits no shares for >2 min → solver isn't producing valid nonces; CPU fallback or arch mismatch likely

If step 5 succeeds for 5 minutes, you're in production. If not, screenshot the miner logs + pool dashboard and DM @shibuyayume.

## What's NOT supported in this build

- **No tuning support yet.** The pool's `/api/worker_solver_recommendation` endpoint hasn't been tested with AMD env vars. You'll get generic CUDA-class advice that may not apply.
- **No per-arch optimization.** Build flags target both gfx1030 + gfx1100 equally; we haven't run benchmarks to find the optimal `SOLVE_BATCH_SIZE` for either RDNA generation.
- **No multi-GPU.** The binary doesn't know how to split work across multiple AMD GPUs in one host. Run one process per GPU with `HIP_VISIBLE_DEVICES=0`, `=1`, etc.

## Feedback

DM **@shibuyayume** on Telegram with:
- Your GPU model + arch (`gfx1030` or `gfx1100`)
- ROCm version (`apt list --installed | grep rocm` or similar)
- N/s observed after 30 min of mining
- Anything else weird

Once we have 2-3 confirmed working setups, this build moves out of `experimental/` into canonical with proper install.sh support.
