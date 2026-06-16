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

    # Solver tuning. Canonical defaults for NVIDIA Pascal through Blackwell;
    # install.sh overrides solver_threads/prepare_workers per detected GPU
    # class (8/12/16). The two key levers are solver_prepare_workers (CPU
    # input generators) and solver_threads (CPU solver workers); bump both
    # first if GPU util sustains below 95%. Keep batch at 128 — it's the
    # sweet spot; 256+ degrades util and 1024 crashes the CUDA buffer pool.
    # See docs/TUNING.md for the per-GPU measured profile table.
    solver_threads: int | None = 8              # BTX_MATMUL_SOLVER_THREADS  (key lever — bump with prepare_workers)
    solver_prepare_workers: int | None = 16     # BTX_MATMUL_PREPARE_WORKERS (key lever — bump with threads)
    solver_batch_size: int | None = 128         # BTX_MATMUL_SOLVE_BATCH_SIZE
    solver_prefetch_depth: int | None = 8       # BTX_MATMUL_PREPARE_PREFETCH_DEPTH
    solver_pipeline_async: int | None = 1       # BTX_MATMUL_PIPELINE_ASYNC (1=overlap prep + kernel)
    solver_backend: str = "cuda"                # BTX_MATMUL_BACKEND (cuda|cpu|metal|mlx)
    gpu_inputs: int | None = 1                  # BTX_MATMUL_GPU_INPUTS (must be 1 post-block-125000 — GPU-gen inputs; saturation on all cards)

    # Ceiling on nonces tried per solver invocation. Deliberately set very high
    # so it NEVER binds — `solver_max_seconds_per_slice` is the real limiter on
    # every GPU class (a 5090 burns ~10^10 nonces in 30s; a 1070 ~2.4x10^9).
    # Keeping max_seconds the binding limit is REQUIRED for GPU saturation: a
    # solver call must run one long, continuous solve so the driver holds the
    # high P-state. (The 2026-06-14 "raising to 4B → 0 shares / ~5300 empty
    # dispatches/sec" incident was the v0.32.10 MatMul-V3 GPU-scan regression
    # — the solver returned 0 because the GPU pre-hash scan was gated off. That
    # is FIXED in v0.32.11; large slices are validated safe there, hours of clean
    # ~1500 N/s on a 1070.) Still a safety cap so a broken solver can't loop a
    # single multi-billion-nonce slice forever.
    nonces_per_slice: int = 100_000_000_000

    # How long a single solver slice runs before returning. THIS is the binding
    # limit and it drives GPU saturation: short slices (the old 5s default)
    # finish before the driver ramps to the high P-state (~12s on a 1070), so the
    # GPU is stuck in a low clock/power state at ~half throughput. A ~30s
    # continuous solve holds P2 (SM ~1800MHz, ~120-150W on a 1070) for the whole
    # slice → ~1.5-2x N/s. Validated on home-1070 (v0.32.11): P5/696MHz/~700 N/s
    # at 5s → P2/~1800MHz/~1500 N/s at 30s.
    #
    # The per-block dead-tip cost of long slices (~17% at 30s if mining a stale
    # parent until the slice ends) is collapsed to ~0 by the SIGUSR1 tip-preempt
    # (see gbt_solve_wrapper.preempt / stratum_client._on_notify): a real parent
    # change aborts the in-flight slice within ~ms. So long slices are now safe.
    # Requires the preempt-capable solver (v0.4.17+); on an older solver the
    # wrapper falls back to letting the slice finish (no SIGUSR1 sent).
    solver_max_seconds_per_slice: float = 30.0

    # Reconnect with exponential backoff bounded by these.
    reconnect_initial_s: float = 1.0
    reconnect_max_s: float = 60.0

    # v0.4.16 (B): how often a running miner re-checks the solver channel for a
    # force-published solver and auto-upgrades (re-exec) without a manual
    # restart. Floored at 300s. Set DEXBTX_NO_SOLVER_RECHECK=1 to disable.
    solver_recheck_interval_secs: float = 1800.0

    # ── v0.4.18 solver auto-heal ────────────────────────────────────────────
    # The long-running solver daemon can latch a bad runtime state (gross-wrong
    # V3 digests, or a hang) — typically around a pool restart's job
    # discontinuity — and then mine 0 valid shares until the *process* is
    # restarted. A pool reconnect does NOT clear it (the daemon persists across
    # stratum reconnects). This watchdog bounces the solver daemon when it
    # detects the wedge; the next slice respawns a fresh daemon. Proven on
    # home-1070 (restart → revived; reconnect/kick → did not).
    #
    # Triggers (work-proportional, so ramp-ups / slow GPUs / high vardiff don't
    # false-trip — neither is a naive wall-clock-since-accept):
    #   B (wrong-digest): N consecutive submitted-and-rejected shares with ZERO
    #     accepts in between. Resets to 0 on ANY accept. A rig must actually FIND
    #     and submit N candidates and have the pool reject ALL — a healthy reject
    #     rate (~0-5%) never reaches N; a cold-start/slow rig hasn't found N yet.
    #   A (hang): the solver returns NO result (slice completion) for this many
    #     seconds while the process is alive. A healthy solver completes a slice
    #     every few seconds, so this is unambiguous (the home-1070 case, where
    #     a/r/b froze and B can never fire because nothing is submitted).
    heal_enabled: bool = True
    heal_consec_rejects: int = 8           # trigger B threshold
    heal_solver_stall_secs: float = 90.0   # trigger A threshold
    heal_first_slice_grace_secs: float = 120.0  # cold-start grace for trigger A
    heal_cooldown_secs: float = 300.0      # min between bounces (escalates if heals don't help)
    heal_check_interval_secs: float = 15.0 # watchdog evaluation cadence

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
