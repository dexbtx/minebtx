"""DEXBTX miner CLI entry point.

Usage:
    dexbtx-miner --pool host:port --address btx1z... [options]

Or with a YAML config:
    dexbtx-miner --config /path/to/miner.yaml

CLI flags override YAML keys. The miner connects, authorizes, and runs until
killed; reconnects with exponential backoff on disconnect.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import sys
from pathlib import Path

from .config import MinerConfig, load_yaml_config
from .solver_updater import SolverUpdateRequired, maybe_update_solver
from .stratum_client import StratumClient
from .wrapper_updater import maybe_self_upgrade

log = logging.getLogger(__name__)


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def _parse_pool(s: str) -> tuple[str, int]:
    if ":" not in s:
        raise argparse.ArgumentTypeError(f"--pool must be host:port, got {s!r}")
    host, port_s = s.rsplit(":", 1)
    return host, int(port_s)


def _build_config(args: argparse.Namespace) -> MinerConfig:
    cfg = MinerConfig()
    if args.config is not None:
        raw = load_yaml_config(args.config)
        for k, v in raw.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
            else:
                log.warning("ignoring unknown config key: %s", k)

    if args.pool is not None:
        cfg.pool_host, cfg.pool_port = _parse_pool(args.pool)
    if args.tls:
        cfg.pool_tls = True
    if args.address is not None:
        cfg.payout_address = args.address
    if args.worker is not None:
        cfg.worker_name = args.worker
    if args.gbt_solve is not None:
        cfg.gbt_solve_path = args.gbt_solve
    if args.threads is not None:
        cfg.solver_threads = args.threads
    if args.batch_size is not None:
        cfg.solver_batch_size = args.batch_size
    if args.prefetch is not None:
        cfg.solver_prefetch_depth = args.prefetch
    if args.prepare_workers is not None:
        cfg.solver_prepare_workers = args.prepare_workers
    if args.gpu_inputs is not None:
        cfg.gpu_inputs = args.gpu_inputs
    if args.nonces_per_slice is not None:
        cfg.nonces_per_slice = args.nonces_per_slice
    if args.log_level is not None:
        cfg.log_level = args.log_level

    if not cfg.payout_address:
        raise SystemExit("payout address is required (--address or YAML payout_address)")
    return cfg


def _make_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="dexbtx-miner")
    ap.add_argument("--config", "-c", type=Path,
                    help="Optional YAML config (CLI flags override its values)")
    ap.add_argument("--pool", help="Pool address as host:port, e.g. stratum.minebtx.com:3333")
    ap.add_argument("--tls", action="store_true", help="Use TLS to the pool")
    ap.add_argument("--address", help="Your btx1z... payout address")
    ap.add_argument("--worker", help="Worker name (default: 'default')")
    ap.add_argument("--gbt-solve", help="Path to btx-gbt-solve binary")
    ap.add_argument("--threads", type=int,
                    help="BTX_MATMUL_SOLVER_THREADS — KEY LEVER (with --prepare-workers); canonical 8")
    ap.add_argument("--prepare-workers", type=int,
                    help="BTX_MATMUL_PREPARE_WORKERS — KEY LEVER (with --threads); canonical 16. "
                         "Bump both together if GPU util is sub-95%%.")
    ap.add_argument("--batch-size", type=int,
                    help="BTX_MATMUL_SOLVE_BATCH_SIZE; canonical 128 (avoid 256 on 5070 Ti)")
    ap.add_argument("--prefetch", type=int,
                    help="BTX_MATMUL_PREPARE_PREFETCH_DEPTH; canonical 8")
    ap.add_argument("--gpu-inputs", type=int,
                    help="BTX_MATMUL_GPU_INPUTS (must be 1 post-block-125000 — "
                         "GPU-gen inputs; required for saturation on every card)")
    ap.add_argument("--nonces-per-slice", type=int,
                    help="Nonces tried per solver invocation before checking for new work")
    ap.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                    help="Log verbosity")
    return ap


async def _run(cfg: MinerConfig) -> int:
    _setup_logging(cfg.log_level)
    log.info("dexbtx-miner starting")
    log.info("  pool=%s:%d tls=%s", cfg.pool_host, cfg.pool_port, cfg.pool_tls)
    log.info("  worker=%s.%s", cfg.payout_address, cfg.worker_name)
    log.info("  solver=%s threads=%s batch=%s gpu_inputs=%s",
             cfg.gbt_solve_path, cfg.solver_threads,
             cfg.solver_batch_size, cfg.gpu_inputs)

    # Auto-update the solver binary against the published manifest before
    # we spawn it. Routine errors fail open (mining continues with current
    # local binary). A `min_required_sha256` mismatch that we can't satisfy
    # raises SolverUpdateRequired and we refuse to start — this is the
    # lever for fork-mandatory upgrades.
    try:
        maybe_update_solver(cfg.gbt_solve_path)
    except SolverUpdateRequired as e:
        log.error("MANDATORY solver upgrade unsatisfied: %s", e)
        return 1

    client = StratumClient(cfg)
    try:
        await client.run_forever()
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("miner stopping")
    log.info("totals: accepted=%d rejected=%d blocks=%d",
             client.shares_accepted, client.shares_rejected, client.blocks_found)
    return 0


def main() -> int:
    # v0.4.1: at process start, check the channel manifest for a newer
    # wrapper. If one is published, pip-upgrade + re-exec — from this
    # version onward operators don't have to re-run install.sh to pick
    # up wrapper improvements. Mirrors solver_updater's fail-open
    # semantics; logs warnings on error, never raises. Bare logging
    # config so the bootstrap path is visible even before _setup_logging
    # in _run runs.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    maybe_self_upgrade()

    # Subcommand dispatch: `dexbtx-miner benchmark ...` runs the benchmark.
    # Everything else (no subcommand or unknown first arg) falls through to
    # the mining argparser, preserving back-compat with `dexbtx-miner --pool ...`.
    if len(sys.argv) >= 2 and sys.argv[1] == "benchmark":
        from dexbtx_miner.benchmark import main as bench_main
        return bench_main(sys.argv[2:])
    args = _make_argparser().parse_args()
    cfg = _build_config(args)
    try:
        return asyncio.run(_run(cfg))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
