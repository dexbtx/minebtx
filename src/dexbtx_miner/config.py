"""Miner configuration.

Loaded from CLI flags + optional YAML config. Sensible defaults so a fresh
install can run with just --pool and --address.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any


@dataclasses.dataclass
class MinerConfig:
    # Pool connection.
    pool_host: str = "127.0.0.1"
    pool_port: int = 3333
    pool_tls: bool = False

    # Worker identity. `payout_address.worker_name` shipped as the stratum
    # worker-name field; the pool extracts the address.
    payout_address: str = ""
    worker_name: str = "default"

    # Path to the upstream BTX solver binary. The install.sh writes this
    # to ~/.dexbtx-miner/bin/btx-gbt-solve on Linux/macOS. Override via
    # config.yaml or the `BTX_GBT_SOLVE` env var if you have a custom build.
    gbt_solve_path: str = str(Path.home() / ".dexbtx-miner" / "bin" / "btx-gbt-solve")

    # Solver tuning. Universal canonical defaults for NVIDIA Pascal through
    # Blackwell. The two key levers are solver_prepare_workers (CPU input
    # generators) and solver_threads (CPU solver workers); bump both first
    # if GPU util sustains below 95%. Batch size has no meaningful effect
    # on steady-state throughput once prep is sized correctly. See
    # docs/TUNING.md for the per-GPU measured profile table.
    solver_threads: int | None = 8              # BTX_MATMUL_SOLVER_THREADS  (key lever — bump with prepare_workers)
    solver_prepare_workers: int | None = 16     # BTX_MATMUL_PREPARE_WORKERS (key lever — bump with threads)
    solver_batch_size: int | None = 128         # BTX_MATMUL_SOLVE_BATCH_SIZE
    solver_prefetch_depth: int | None = 8       # BTX_MATMUL_PREPARE_PREFETCH_DEPTH
    solver_pipeline_async: int | None = 1       # BTX_MATMUL_PIPELINE_ASYNC (1=overlap prep + kernel)
    solver_backend: str = "cuda"                # BTX_MATMUL_BACKEND (cuda|cpu|metal|mlx)
    gpu_inputs: int | None = 0                  # BTX_MATMUL_GPU_INPUTS (must be 0 — the saturation breakthrough)

    # Ceiling on nonces tried per solver invocation. In practice this is
    # rarely the binding limit — `solver_max_seconds_per_slice` bounds
    # each slice first on every modern GPU. Effectively a safety cap so a
    # broken solver can't run away with a single multi-billion-nonce slice.
    nonces_per_slice: int = 20_000_000

    # How long a single solver slice runs before returning. Shorter slices
    # mean LESS wasted work when a new block lands network-wide and the
    # pool sends us a fresh job — the miner can re-check `_current_job`
    # between slices and abandon stale work sooner.
    #
    # At BTX's 90s target block time, a 5s slice caps the per-block waste
    # at ~5.5% (vs ~17% with the old 30s default). With daemon mode the
    # per-slice overhead is ~ms (CUDA context persists across slices), so
    # shorter slices are essentially free.
    #
    # Don't go below ~2s: the kernel needs enough wall time to amortise
    # the per-launch dispatch cost on slower cards (Pascal-class).
    solver_max_seconds_per_slice: float = 5.0

    # Reconnect with exponential backoff bounded by these.
    reconnect_initial_s: float = 1.0
    reconnect_max_s: float = 60.0

    log_level: str = "INFO"


def fully_qualified_worker(cfg: MinerConfig) -> str:
    """The `address.worker_name` string sent on mining.authorize."""
    if not cfg.payout_address:
        raise ValueError("payout_address must be set")
    return f"{cfg.payout_address}.{cfg.worker_name}"


def load_yaml_config(path: str | Path) -> dict[str, Any]:
    """Optional YAML override layer. CLI flags trump YAML."""
    try:
        import yaml
    except ImportError as e:
        raise RuntimeError("pyyaml required for --config; pip install pyyaml") from e
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"config not found: {p}")
    with open(p) as f:
        return yaml.safe_load(f) or {}
