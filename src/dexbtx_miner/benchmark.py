"""Benchmark the local btx-gbt-solve binary at varying env-var settings.

Measures **raw nonce throughput** (nonces/sec) at each config and picks the
fastest. How it works: run the solver on a deterministic test job for a fixed
wall-clock window with a share target so tight it's never hit, so the solver
never early-exits — it scans nonces for the full duration and reports
`tries_used` (the count of nonce attempts it made). Throughput is then simply
`tries_used / elapsed_s`. This is an honest, repeatable N/s for *relative*
comparison across configs on your hardware. (For your true pool-credited rate,
the 18-bit pre-hash gate makes share-rate lower than raw nonce-rate; see
btxprice.com for the on-chain-derived figure.)

This replaces the older time-to-find-a-share metric, which was dominated by
process/GPU-init startup and the fixed winning-nonce position, and so
mis-ranked configs (e.g. reporting fewer threads as "faster").

The sweep is platform-aware:
  - Apple Silicon: sweeps backend (mlx vs metal) x solver-threads (the
    dominant lever — the solver is CPU-prep-bound, so more cores = more N/s
    up to hw.ncpu). batch/prefetch/workers are CUDA-centric and held fixed.
  - NVIDIA/CPU: sweeps batch/prefetch/workers (the CUDA prep-pipeline levers).

Usage:
    dexbtx-miner benchmark                  # platform-aware sweep, 30s each
    dexbtx-miner benchmark --write-config   # write the winner to config.yaml
    dexbtx-miner benchmark --threads 4,8,10 --backend mlx,metal
    dexbtx-miner benchmark --batches 16,32,64,128 --duration 60
    dexbtx-miner benchmark --gbt-solve /path/to/btx-gbt-solve
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Deterministic test job. block_height is past the V2 nonce-seed activation
# (125000) so the benchmark exercises the SAME per-nonce seed-derivation path
# that real mining uses today — i.e. it measures real throughput, not the
# cheaper pre-fork path. Pure reference: doesn't talk to the pool.
TEST_JOB = {
    "version": 536870912,
    "prev_hash": "0ab38fdff2ef667dcddac7f50c3696080c26697615f7b6b9af5c3a1ba0a5fb7e",
    "merkle_root": "d906f02ed11d8936770423263b56c5ffe1ea1b15c8a2867afb161adb6fd76eb7",
    "time": 1779672814,
    "bits": "0x1d17c609",
    "seed_a": "8460daf3ff446cc55a7115de88ee24c8a2bf182eedde43abb9cf4cc94cc209bf",
    "seed_b": "7f2e377616feb92d2e9857cab390595b7d6b8d24373a2da394f8d97197b5f437",
    "block_height": 130000,
    "matmul_n": 512, "matmul_b": 16, "matmul_r": 8, "epsilon_bits": 18,
}

# Share target tight enough to never be hit (digest <= 1 is astronomically
# unlikely) but NON-ZERO — the solver refuses a zero target. This forces the
# solver to scan the full --max-seconds window so tries_used reflects the
# whole duration's work.
UNHITTABLE_TARGET = "0000000000000000000000000000000000000000000000000000000000000001"


@dataclass
class BenchResult:
    label: str
    env: dict[str, str]
    nonces_per_sec: float
    tries_used: int
    elapsed_s: float
    ok: bool

    def as_row(self) -> str:
        if not self.ok:
            return f"{self.label:<58}  FAILED (no usable solver output)"
        return (f"{self.label:<58}  {self.nonces_per_sec:>12,.0f} N/s"
                f"   ({self.tries_used:,} nonces / {self.elapsed_s:.1f}s)")


def run_one(solver_bin: str, env_kv: dict[str, str], duration: float, nonce_start: int = 1) -> BenchResult:
    """Run one solver invocation for `duration` seconds; return its nonce throughput."""
    label = " ".join(f"{k.replace('BTX_MATMUL_', '').lower()}={v}"
                     for k, v in env_kv.items() if v is not None)
    env = dict(os.environ)
    env.update(env_kv)

    cmd = [
        solver_bin,
        "--version", str(TEST_JOB["version"]),
        "--prev-hash", TEST_JOB["prev_hash"],
        "--merkle-root", TEST_JOB["merkle_root"],
        "--time", str(TEST_JOB["time"]),
        "--bits", TEST_JOB["bits"],
        "--share-target", UNHITTABLE_TARGET,
        "--seed-a", TEST_JOB["seed_a"],
        "--seed-b", TEST_JOB["seed_b"],
        "--block-height", str(TEST_JOB["block_height"]),
        "--matmul-n", str(TEST_JOB["matmul_n"]),
        "--matmul-b", str(TEST_JOB["matmul_b"]),
        "--matmul-r", str(TEST_JOB["matmul_r"]),
        "--epsilon-bits", str(TEST_JOB["epsilon_bits"]),
        "--nonce-start", str(nonce_start),
        "--max-tries", "1000000000000",   # effectively unbounded; max-seconds dominates
        "--max-seconds", f"{duration:.3f}",
        "--backend", env_kv.get("BTX_MATMUL_BACKEND", "cpu"),
    ]
    if env_kv.get("BTX_MATMUL_SOLVE_BATCH_SIZE"):
        cmd += ["--batch-size", str(env_kv["BTX_MATMUL_SOLVE_BATCH_SIZE"])]
    if env_kv.get("BTX_MATMUL_SOLVER_THREADS"):
        cmd += ["--solver-threads", str(env_kv["BTX_MATMUL_SOLVER_THREADS"])]

    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                              timeout=duration + 60)
    except subprocess.TimeoutExpired:
        return BenchResult(label, env_kv, 0.0, 0, duration + 60, False)

    rec: dict | None = None
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
    if rec is None:
        return BenchResult(label, env_kv, 0.0, 0, 0.0, False)

    tries_used = int(rec.get("tries_used", 0))
    elapsed_s = float(rec.get("elapsed_s", 0.0)) or 0.0
    nps = (tries_used / elapsed_s) if elapsed_s > 0 else 0.0
    ok = tries_used > 0 and elapsed_s > 0
    return BenchResult(label, env_kv, nps, tries_used, elapsed_s, ok)


def sweep(solver_bin: str, duration: float, backends: list[str], threads: list[int],
          batches: list[int], prefetches: list[int], workers: list[int],
          pipeline_async: int, gpu_inputs: int) -> list[BenchResult]:
    results = []
    for backend in backends:
        for t in threads:
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
                            "BTX_MATMUL_SOLVER_THREADS": str(t),
                        }
                        print(f"  benchmarking: backend={backend} threads={t} batch={batch} "
                              f"prefetch={prefetch} workers={w} ...", flush=True)
                        r = run_one(solver_bin, env_kv, duration)
                        print(f"    → {r.as_row()}", flush=True)
                        results.append(r)
    return results


def _default_sweep(is_mac: bool, ncpu: int) -> dict[str, str]:
    """Platform-aware default sweep dimensions (as comma-separated strings)."""
    if is_mac:
        # threads is the dominant lever on Apple Silicon; sweep a few up to ncpu.
        tset = sorted({4, max(2, ncpu // 2), ncpu})
        return {
            "backend": "mlx,metal",
            "threads": ",".join(str(t) for t in tset),
            "batches": "128",
            "prefetches": "8",
            "workers": "8",
        }
    return {
        "backend": "cuda",
        "threads": "4",
        "batches": "32,64,128,256",
        "prefetches": "8",
        "workers": "8",
    }


def main(argv: list[str] | None = None) -> int:
    is_mac = platform.system() == "Darwin"
    ncpu = os.cpu_count() or 4
    d = _default_sweep(is_mac, ncpu)

    ap = argparse.ArgumentParser(prog="dexbtx-miner benchmark")
    ap.add_argument("--gbt-solve", default=os.path.expanduser("~/.dexbtx-miner/bin/btx-gbt-solve"),
                    help="Path to btx-gbt-solve")
    ap.add_argument("--duration", type=float, default=30.0,
                    help="Wall-clock seconds per config (default 30)")
    ap.add_argument("--backend", default=d["backend"],
                    help=f"Backend(s) to try, comma-separated (default {d['backend']})")
    ap.add_argument("--threads", default=d["threads"],
                    help=f"Solver thread counts to try, comma-separated (default {d['threads']})")
    ap.add_argument("--batches", default=d["batches"],
                    help=f"Batch sizes to try, comma-separated (default {d['batches']})")
    ap.add_argument("--prefetches", default=d["prefetches"],
                    help="Prefetch depths, comma-separated")
    ap.add_argument("--workers", default=d["workers"],
                    help="Prepare workers, comma-separated")
    ap.add_argument("--pipeline-async", type=int, default=1)
    ap.add_argument("--gpu-inputs", type=int, default=1)
    ap.add_argument("--write-config", action="store_true",
                    help="On success, rewrite ~/.dexbtx-miner/config.yaml with the highest-throughput config")
    args = ap.parse_args(argv)

    if not Path(args.gbt_solve).is_file():
        print(f"error: solver not found at {args.gbt_solve}", file=sys.stderr)
        return 1

    backends = [b.strip() for b in args.backend.split(",") if b.strip()]
    threads = [int(t) for t in args.threads.split(",")]
    batches = [int(b) for b in args.batches.split(",")]
    prefetches = [int(p) for p in args.prefetches.split(",")]
    workers = [int(w) for w in args.workers.split(",")]
    n_configs = len(backends) * len(threads) * len(batches) * len(prefetches) * len(workers)

    print(f"\ndexbtx-miner benchmark  ({platform.system()}, {ncpu} cores)")
    print(f"  solver:   {args.gbt_solve}")
    print(f"  duration: {args.duration}s per config  ({n_configs} configs ≈ "
          f"{n_configs * args.duration / 60:.1f} min)")
    print(f"  sweep:    backend={backends} threads={threads} batches={batches} "
          f"prefetches={prefetches} workers={workers}")
    print(f"  metric:   raw nonce throughput (tries_used / elapsed_s), higher = better\n")

    results = sweep(args.gbt_solve, args.duration, backends, threads, batches,
                    prefetches, workers, args.pipeline_async, args.gpu_inputs)

    rated = [r for r in results if r.ok]
    rated.sort(key=lambda r: r.nonces_per_sec, reverse=True)

    print("\n" + "=" * 100)
    print("Results (sorted by throughput, fastest first). For pool-credited rate see btxprice.com.")
    print("=" * 100)
    if not rated:
        print("  No config produced usable output. Check the solver runs: "
              f"`{args.gbt_solve} --help`.")
        for r in results:
            print(f"  {r.as_row()}")
        return 1
    for r in rated:
        print(f"  {r.as_row()}")

    best = rated[0]
    print(f"\nBest config: {best.label}")
    print(f"  {best.nonces_per_sec:,.0f} N/s")
    if len(rated) > 1 and rated[-1].nonces_per_sec > 0:
        ratio = best.nonces_per_sec / rated[-1].nonces_per_sec
        print(f"  ({ratio:.2f}× the slowest config tested)")

    if args.write_config:
        cfg_path = Path.home() / ".dexbtx-miner" / "config.yaml"
        if not cfg_path.exists():
            print(f"warning: {cfg_path} does not exist; cannot --write-config")
            return 0
        try:
            import yaml
        except ImportError:
            print("error: --write-config requires pyyaml")
            return 2
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
        cfg["solver_backend"] = best.env["BTX_MATMUL_BACKEND"]
        cfg["solver_threads"] = int(best.env["BTX_MATMUL_SOLVER_THREADS"])
        cfg["solver_batch_size"] = int(best.env["BTX_MATMUL_SOLVE_BATCH_SIZE"])
        cfg["solver_prefetch_depth"] = int(best.env["BTX_MATMUL_PREPARE_PREFETCH_DEPTH"])
        cfg["solver_prepare_workers"] = int(best.env["BTX_MATMUL_PREPARE_WORKERS"])
        cfg["solver_pipeline_async"] = int(best.env["BTX_MATMUL_PIPELINE_ASYNC"])
        cfg["gpu_inputs"] = int(best.env["BTX_MATMUL_GPU_INPUTS"])
        with open(cfg_path, "w") as f:
            yaml.safe_dump(cfg, f, sort_keys=False)
        print(f"  wrote {cfg_path} (backend={cfg['solver_backend']} threads={cfg['solver_threads']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
