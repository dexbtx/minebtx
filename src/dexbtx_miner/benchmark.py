"""Benchmark the local btx-gbt-solve binary at varying env-var settings.

This is a RELATIVE-only "find your sweet spot" tool. It measures
**time-to-find-a-deterministic-share** at each env-var combo. Faster =
better. The absolute N/s figure cannot be computed honestly from this
data (the solver's `nonce64` field is the position of the winning nonce,
not the count of nonces tested — there's no reliable way to convert
that to a hashrate without knowing the kernel's internal batch/parallel
factor). For your TRUE hashrate, use btxprice.com — it derives N/s from
on-chain work done over a window, which is the only honest measure.

How it measures: run the solver on a deterministic test job (fixed
seeds + share target) for a fixed wall-clock window. The winning nonce
is constant across configs; only the time-to-find varies. The config
that finds it fastest is the right config for your hardware.

Usage:
    dexbtx-miner benchmark
        # Defaults: 30s per config, sweeps batch sizes 32/64/128/256
        # Reports nonces/sec for each. Picks the best.

    dexbtx-miner benchmark --batches 16,32,64,128
    dexbtx-miner benchmark --duration 60          # longer per-config window
    dexbtx-miner benchmark --gbt-solve /path/to/btx-gbt-solve
    dexbtx-miner benchmark --write-config         # update ~/.dexbtx-miner/config.yaml with the winner
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Deterministic test job (a real historical job from the BTX chain). Pure
# reference: doesn't talk to the pool, doesn't credit anything.
TEST_JOB = {
    "version": 536870912,
    "prev_hash": "0ab38fdff2ef667dcddac7f50c3696080c26697615f7b6b9af5c3a1ba0a5fb7e",
    "merkle_root": "d906f02ed11d8936770423263b56c5ffe1ea1b15c8a2867afb161adb6fd76eb7",
    "time": 1779672814,
    "bits": "0x1d17c609",
    "seed_a": "8460daf3ff446cc55a7115de88ee24c8a2bf182eedde43abb9cf4cc94cc209bf",
    "seed_b": "7f2e377616feb92d2e9857cab390595b7d6b8d24373a2da394f8d97197b5f437",
    "block_height": 110806,
    "matmul_n": 512, "matmul_b": 16, "matmul_r": 8, "epsilon_bits": 18,
    # Loose share target so the solver returns within a few seconds. Each
    # benchmark slice that finds a share gives us a real `nonce64` ceiling
    # to extrapolate from.
    "share_target": "00ffffff00000000000000000000000000000000000000000000000000000000",
}


@dataclass
class BenchResult:
    label: str                 # e.g. "batch=32 prefetch=8 workers=8 async=1"
    env: dict[str, str]
    found: bool
    nonce_end: int | None      # solver's winning-nonce position (NOT a count)
    tries_used: int            # raw counter; useful for tooling debug
    elapsed_s: float

    def as_row(self) -> str:
        # Time-to-find is the honest metric. Smaller = faster solver. Do NOT
        # divide nonce_end by elapsed_s — that's not a real N/s.
        if self.found:
            return f"{self.label:<60}  time-to-find={self.elapsed_s:>6.1f}s"
        else:
            return f"{self.label:<60}  NO HIT in {self.elapsed_s:.1f}s (slower than this run's duration)"


def run_one(solver_bin: str, env_kv: dict[str, str], duration: float, nonce_start: int = 1) -> BenchResult:
    """Run a single solver invocation with the given env. Return real N/s."""
    label = " ".join(f"{k.replace('BTX_MATMUL_', '')}={v}" for k, v in env_kv.items() if v is not None)
    env = dict(os.environ)
    env.update(env_kv)

    cmd = [
        solver_bin,
        "--version", str(TEST_JOB["version"]),
        "--prev-hash", TEST_JOB["prev_hash"],
        "--merkle-root", TEST_JOB["merkle_root"],
        "--time", str(TEST_JOB["time"]),
        "--bits", TEST_JOB["bits"],
        "--share-target", TEST_JOB["share_target"],
        "--seed-a", TEST_JOB["seed_a"],
        "--seed-b", TEST_JOB["seed_b"],
        "--block-height", str(TEST_JOB["block_height"]),
        "--matmul-n", str(TEST_JOB["matmul_n"]),
        "--matmul-b", str(TEST_JOB["matmul_b"]),
        "--matmul-r", str(TEST_JOB["matmul_r"]),
        "--epsilon-bits", str(TEST_JOB["epsilon_bits"]),
        "--nonce-start", str(nonce_start),
        "--max-tries", "1000000000",   # large; let max-seconds dominate
        "--max-seconds", f"{duration:.3f}",
        "--backend", env_kv.get("BTX_MATMUL_BACKEND", "cuda"),
    ]
    # Pass batch + threads explicitly via CLI too (they take precedence)
    if env_kv.get("BTX_MATMUL_SOLVE_BATCH_SIZE"):
        cmd += ["--batch-size", str(env_kv["BTX_MATMUL_SOLVE_BATCH_SIZE"])]
    if env_kv.get("BTX_MATMUL_SOLVER_THREADS"):
        cmd += ["--solver-threads", str(env_kv["BTX_MATMUL_SOLVER_THREADS"])]

    t0 = time.monotonic()
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                              timeout=duration + 30)
    except subprocess.TimeoutExpired:
        return BenchResult(label, env_kv, False, None, 0, duration + 30, None)
    elapsed = time.monotonic() - t0

    rec: dict | None = None
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
    if rec is None:
        return BenchResult(label, env_kv, False, None, 0, elapsed, None)

    found = bool(rec.get("found"))
    tries_used = int(rec.get("tries_used", 0))
    elapsed_s = float(rec.get("elapsed_s", elapsed))
    nonce_end = int(rec.get("nonce64", 0)) if found else None
    return BenchResult(label, env_kv, found, nonce_end, tries_used, elapsed_s)


def sweep(solver_bin: str, duration: float, batches: list[int],
          prefetches: list[int], workers: list[int],
          pipeline_async: int, gpu_inputs: int,
          backend: str, threads: int) -> list[BenchResult]:
    results = []
    for batch in batches:
        for prefetch in prefetches:
            for w in workers:
                env_kv = {
                    "BTX_MATMUL_BACKEND": backend,
                    "BTX_MATMUL_GPU_INPUTS": str(gpu_inputs),
                    "BTX_MATMUL_SOLVE_BATCH_SIZE": str(batch),
                    "BTX_MATMUL_PREPARE_PREFETCH_DEPTH": str(prefetch),
                    "BTX_MATMUL_PREPARE_WORKERS": str(w),
                    "BTX_MATMUL_PIPELINE_ASYNC": str(pipeline_async),
                    "BTX_MATMUL_SOLVER_THREADS": str(threads),
                }
                print(f"  benchmarking: batch={batch} prefetch={prefetch} workers={w} ...", flush=True)
                r = run_one(solver_bin, env_kv, duration)
                print(f"    → {r.as_row()}", flush=True)
                results.append(r)
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="dexbtx-miner benchmark")
    ap.add_argument("--gbt-solve", default=os.path.expanduser("~/.dexbtx-miner/bin/btx-gbt-solve"),
                    help="Path to btx-gbt-solve")
    ap.add_argument("--duration", type=float, default=30.0,
                    help="Wall-clock seconds per config (default 30)")
    ap.add_argument("--batches", default="32,64,128,256",
                    help="Batch sizes to try, comma-separated")
    ap.add_argument("--prefetches", default="8",
                    help="Prefetch depths to try, comma-separated (default 8)")
    ap.add_argument("--workers", default="8",
                    help="Prepare workers to try, comma-separated (default 8)")
    ap.add_argument("--pipeline-async", type=int, default=1,
                    help="1=async, 0=sync (default 1)")
    ap.add_argument("--gpu-inputs", type=int, default=0,
                    help="0=CPU-gen inputs (Pascal fix), 1=GPU-gen (default 0)")
    ap.add_argument("--backend", default="cuda")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--write-config", action="store_true",
                    help="On success, rewrite ~/.dexbtx-miner/config.yaml with the winning settings")
    args = ap.parse_args(argv)

    if not Path(args.gbt_solve).is_file():
        print(f"error: solver not found at {args.gbt_solve}", file=sys.stderr)
        return 1

    batches = [int(b) for b in args.batches.split(",")]
    prefetches = [int(p) for p in args.prefetches.split(",")]
    workers = [int(w) for w in args.workers.split(",")]

    print(f"\ndexbtx-miner benchmark")
    print(f"  solver: {args.gbt_solve}")
    print(f"  duration: {args.duration}s per config")
    print(f"  sweep:  batches={batches} prefetches={prefetches} workers={workers}")
    print(f"  fixed:  backend={args.backend} pipeline_async={args.pipeline_async} "
          f"gpu_inputs={args.gpu_inputs} threads={args.threads}")
    print()

    results = sweep(args.gbt_solve, args.duration, batches, prefetches, workers,
                    args.pipeline_async, args.gpu_inputs, args.backend, args.threads)

    print("\n" + "=" * 100)
    print("Results (sorted by time-to-find, fastest first). Faster = better config for your hardware.")
    print("For absolute N/s see btxprice.com (derived from on-chain work).")
    print("=" * 100)
    rated = [r for r in results if r.found]
    rated.sort(key=lambda r: r.elapsed_s)
    if not rated:
        print("  No configs found a share within the duration. Try --duration 60 or --batches 16,32.")
        print("  All results (unsorted):")
        for r in results:
            print(f"  {r.as_row()}")
        return 1
    for r in rated:
        print(f"  {r.as_row()}")

    best = rated[0]
    print(f"\nBest config: {best.label}")
    print(f"  Found the deterministic test share in {best.elapsed_s:.1f}s")
    if len(rated) > 1:
        ratio = rated[-1].elapsed_s / best.elapsed_s
        print(f"  ({ratio:.2f}× faster than the slowest config tested)")

    if args.write_config:
        cfg_path = Path.home() / ".dexbtx-miner" / "config.yaml"
        if not cfg_path.exists():
            print(f"warning: {cfg_path} does not exist; cannot --write-config")
            return 0
        try:
            import yaml
        except ImportError:
            print(f"error: --write-config requires pyyaml")
            return 2
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
        cfg["solver_batch_size"] = int(best.env["BTX_MATMUL_SOLVE_BATCH_SIZE"])
        cfg["solver_prefetch_depth"] = int(best.env["BTX_MATMUL_PREPARE_PREFETCH_DEPTH"])
        cfg["solver_prepare_workers"] = int(best.env["BTX_MATMUL_PREPARE_WORKERS"])
        cfg["solver_pipeline_async"] = int(best.env["BTX_MATMUL_PIPELINE_ASYNC"])
        cfg["gpu_inputs"] = int(best.env["BTX_MATMUL_GPU_INPUTS"])
        cfg["solver_threads"] = int(best.env["BTX_MATMUL_SOLVER_THREADS"])
        cfg["solver_backend"] = best.env["BTX_MATMUL_BACKEND"]
        with open(cfg_path, "w") as f:
            yaml.safe_dump(cfg, f, sort_keys=False)
        print(f"  wrote {cfg_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
