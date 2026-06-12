"""Async wrapper around BTX's patched `btx-gbt-solve` solver, in **daemon mode**.

Requires a patched solver build with TWO flags upstream doesn't ship:
  --daemon         : keep the CUDA context + cubins loaded across slices,
                     reading per-slice JSON jobs on stdin and emitting one
                     JSON result line on stdout. Eliminates the ~5s
                     CUDA-context-init cost that would otherwise apply
                     every slice.
  --share-target   : exit early on `digest <= share_target` instead of
                     only on `digest <= block_target` (block target is
                     impossibly hard at the per-miner share rate; without
                     this flag, the solver runs until max_seconds, never
                     returning a found share).

`install.sh` verifies both flags are present and aborts if either is
missing — there is no fallback path for a stock-upstream binary.

Daemon-mode protocol (read this code's `solve_slice` for the JSON shape):
  - one job at a time per process (guarded by `_daemon_lock`)
  - one result JSON per job, on stdout
  - solver daemon prints `{"event":"daemon_ready"}` on stderr at startup
  - if the daemon dies mid-job we respawn once and retry; persistent
    failure surfaces as SolveResult(found=False, tries_used=0)

The `--bits` we pass is the BLOCK target (header.nBits is hashed into the
matmul digest). The patched solver does the share-tier early exit via
`--share-target`, not by lowering --bits. The pool server validates each
returned share against both the share target (for credit) and the block
target (for submitblock).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclasses.dataclass
class SolveChallenge:
    """The 8 required + optional matmul params that define a solver slice."""
    version: int
    prev_hash: str          # hex64, big-endian display
    merkle_root: str        # hex64, big-endian display
    time: int               # nTime (uint32)
    bits: str               # BLOCK target (compact, e.g. "1d17c609"); sets header.nBits
    seed_a: str             # hex64, big-endian display
    seed_b: str             # hex64, big-endian display
    block_height: int       # block height for the consensus-fork-aware path
    matmul_n: int = 512
    matmul_b: int = 16
    matmul_r: int = 8
    epsilon_bits: int = 18
    # Optional: 256-bit BE share target hex. When set, the solver exits on
    # digest <= share_target instead of the block target. Requires the
    # patched btx-gbt-solve binary (--share-target flag, June 2026+).
    share_target_hex: str | None = None


@dataclasses.dataclass
class SolveResult:
    """Outcome of one solve slice."""
    found: bool
    tries_used: int
    elapsed_s: float
    nonce: int | None = None
    digest_hex: str | None = None   # the matmul digest (BE display form)
    ntime: int | None = None         # nTime used for this result (== challenge.time in v1)
    raw_output: dict[str, Any] | None = None  # full JSON record
    # True when the digest ALSO beats the block target — caller should submit
    # to the chain. Only meaningful with the patched solver supporting
    # --share-target. Defaults to True so legacy/unpatched solvers (which
    # only exit at block target anyway) keep working without changes.
    is_block: bool = True
    # Final solver nonce position (header.nNonce64 at exit). Emitted by the
    # patched solver always (found or not). Crucial for advancing nonce_start
    # correctly — `tries_used` is an internal counter that under-counts real
    # nonces scanned by 4-5 orders of magnitude on modern GPUs. None if the
    # solver doesn't emit this field (legacy/unpatched binary).
    nonce_end: int | None = None


@dataclasses.dataclass
class SolverEnv:
    """Tunables injected as env-vars into the btx-gbt-solve subprocess.

    Env-var names match the upstream BTX matmul source — verified via
    `grep getenv` in src/matmul/. Setting wrong names silently no-ops,
    which is how we shipped a broken `BTX_MATMUL_PREFETCH` for a while.
    """
    batch_size: int | None = None              # BTX_MATMUL_SOLVE_BATCH_SIZE
    prefetch_depth: int | None = None          # BTX_MATMUL_PREPARE_PREFETCH_DEPTH
    prepare_workers: int | None = None         # BTX_MATMUL_PREPARE_WORKERS
    pipeline_async: int | None = None          # BTX_MATMUL_PIPELINE_ASYNC (0/1)
    gpu_inputs: int | None = None              # BTX_MATMUL_GPU_INPUTS (1=GPU-gen, mandatory post-block-125000; 0 was the pre-fork value)
    solver_threads: int | None = None          # BTX_MATMUL_SOLVER_THREADS
    backend: str | None = None                 # BTX_MATMUL_BACKEND (cuda/cpu/metal/mlx)
    pool_slots: int | None = None
    extra: dict[str, str] = dataclasses.field(default_factory=dict)

    def to_env(self, base: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(base) if base is not None else dict(os.environ)
        # v0.32.3 gates CUDA on sm_80+ by default; this env var opt-in lowers
        # the floor to sm_60 (Pascal 10xx, Turing 16xx/20xx). No-op on Ampere+.
        # Set unconditionally — the upstream binary ignores the env var when
        # the GPU is already supported.
        env.setdefault("BTX_CUDA_ALLOW_OLDER_GPUS", "1")
        # Post-block-125000 the matmul seed folds nTime. The solver auto-
        # refreshes the header time mid-search (default every 4096 attempts),
        # so a found share's digest is for a time the pool never sees -> the
        # pool recomputes against job.time and rejects code-23. Frequent on
        # slower rigs post-fork (more attempts/share). Disable so the solver
        # mines the exact job header. (Parser rejects 0; max u32 = never.)
        env.setdefault("BTX_MINER_HEADER_TIME_REFRESH_ATTEMPTS", "4294967295")
        if self.backend is not None:
            env["BTX_MATMUL_BACKEND"] = str(self.backend)
        if self.batch_size is not None:
            env["BTX_MATMUL_SOLVE_BATCH_SIZE"] = str(self.batch_size)
        if self.prefetch_depth is not None:
            env["BTX_MATMUL_PREPARE_PREFETCH_DEPTH"] = str(self.prefetch_depth)
        if self.prepare_workers is not None:
            env["BTX_MATMUL_PREPARE_WORKERS"] = str(self.prepare_workers)
        if self.pipeline_async is not None:
            env["BTX_MATMUL_PIPELINE_ASYNC"] = str(self.pipeline_async)
        if self.gpu_inputs is not None:
            env["BTX_MATMUL_GPU_INPUTS"] = str(self.gpu_inputs)
        if self.solver_threads is not None:
            env["BTX_MATMUL_SOLVER_THREADS"] = str(self.solver_threads)
        env.update(self.extra)
        return env


class GbtSolveWrapper:
    """Drive upstream btx-gbt-solve in **daemon mode**.

    One long-running solver subprocess is spawned at __init__ and reused for
    every slice. Per-slice work is sent as a JSON line on the daemon's stdin;
    one JSON result line comes back on stdout. This eliminates ~5s of
    CUDA-context-init + cubin-load per slice (the cost that capped duty
    cycle at ~75% on the per-subprocess model).

    Requires btx-gbt-solve with the `--daemon` flag (built from
    src/btx-gbt-solve.cpp at the daemon-mode patch level or later). If the
    binary doesn't support `--daemon`, `_ensure_daemon` will raise.
    """

    def __init__(
        self,
        gbt_solve_path: str,
        *,
        backend: str = "cuda",
        solver_threads: int = 8,
        batch_size: int = 128,
        solver_env: SolverEnv | None = None,
    ):
        self.gbt_solve_path = gbt_solve_path
        self.backend = backend
        self.solver_threads = solver_threads
        self.batch_size = batch_size
        self.solver_env = solver_env or SolverEnv()
        self._daemon: asyncio.subprocess.Process | None = None
        self._daemon_lock = asyncio.Lock()
        self._matmul_params: dict[str, int] | None = None
        self._verify_binary()

    def _verify_binary(self) -> None:
        p = Path(self.gbt_solve_path)
        if not p.exists():
            log.warning(
                "btx-gbt-solve not found at %s — solver will fail at run time",
                self.gbt_solve_path,
            )

    async def _ensure_daemon(self, challenge: SolveChallenge) -> asyncio.subprocess.Process:
        """Lazy-spawn the daemon subprocess on first use. Matmul-shape args
        are session-level (locked at startup), so we record the first
        challenge's shape and re-spawn the daemon if a later challenge has
        a different matmul_n/b/r/epsilon_bits — should never happen on a
        single network/chain, but guards against silent consensus drift."""
        shape = {
            "matmul_n": challenge.matmul_n,
            "matmul_b": challenge.matmul_b,
            "matmul_r": challenge.matmul_r,
            "epsilon_bits": challenge.epsilon_bits,
        }
        if self._daemon is not None and self._matmul_params != shape:
            log.warning("matmul shape changed (%s -> %s); respawning daemon",
                        self._matmul_params, shape)
            await self._shutdown_daemon()
        if self._daemon is not None and self._daemon.returncode is None:
            return self._daemon

        cmd: list[str] = [
            self.gbt_solve_path,
            "--matmul-n", str(challenge.matmul_n),
            "--matmul-b", str(challenge.matmul_b),
            "--matmul-r", str(challenge.matmul_r),
            "--epsilon-bits", str(challenge.epsilon_bits),
            "--daemon",
        ]
        if self.backend:
            cmd += ["--backend", self.backend]
        if self.solver_threads:
            cmd += ["--solver-threads", str(self.solver_threads)]
        if self.batch_size:
            cmd += ["--batch-size", str(self.batch_size)]

        env = self.solver_env.to_env()
        log.info("spawning solver daemon: %s", " ".join(cmd))
        # Bump StreamReader buffer to 16 MiB so found-share JSON lines fit.
        # The default 64KB cannot hold matrix_c_data_hex (512×512 matrix
        # → ~2 MiB hex). Without this, every found-share crashes the reader
        # with "Separator is not found, and chunk exceed the limit" and we
        # respawn the daemon — losing all the cost savings the persistent
        # process was supposed to provide.
        self._daemon = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
        )
        # Wait for daemon_ready handshake on stderr (with timeout — if the
        # binary lacks --daemon support or fails to init CUDA, we want to
        # surface that quickly instead of hanging on stdin write).
        assert self._daemon.stderr is not None
        try:
            ready_line = await asyncio.wait_for(self._daemon.stderr.readline(), timeout=30.0)
        except asyncio.TimeoutError as e:
            await self._shutdown_daemon()
            raise RuntimeError("solver daemon did not signal ready within 30s") from e
        ready_text = ready_line.decode(errors="replace").strip()
        if "daemon_ready" not in ready_text:
            log.warning("unexpected daemon ready line: %r", ready_text)
        else:
            log.info("solver daemon ready: %s", ready_text)
        self._matmul_params = shape
        return self._daemon

    async def _shutdown_daemon(self) -> None:
        if self._daemon is None:
            return
        try:
            if self._daemon.stdin is not None and not self._daemon.stdin.is_closing():
                self._daemon.stdin.close()
            await asyncio.wait_for(self._daemon.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            # Daemon ignored stdin close — escalate to SIGKILL.
            try:
                self._daemon.kill()
                await self._daemon.wait()
            except ProcessLookupError:
                pass  # already exited between timeout and kill
        self._daemon = None
        self._matmul_params = None

    async def solve_slice(
        self,
        challenge: SolveChallenge,
        *,
        nonce_start: int,
        max_tries: int,
        max_seconds: float = 0.0,
    ) -> SolveResult:
        """Send one job to the persistent solver daemon; await its result line.

        The daemon stays alive between slices, preserving the CUDA context
        and loaded cubins — eliminating per-slice startup cost.

        If the daemon dies mid-job we respawn once and retry; persistent
        failure surfaces as `SolveResult(found=False, tries_used=0)`.
        """
        bits_str = challenge.bits[2:] if challenge.bits.startswith("0x") else challenge.bits
        job: dict[str, Any] = {
            "version": challenge.version,
            "prev_hash": challenge.prev_hash,
            "merkle_root": challenge.merkle_root,
            "time": challenge.time,
            "bits": bits_str,
            "seed_a": challenge.seed_a,
            "seed_b": challenge.seed_b,
            "block_height": challenge.block_height,
            "nonce_start": nonce_start,
            "max_tries": max_tries,
            # Default to 5s rather than 30s — at BTX's 90s target block
            # time, longer slices are pure waste on `clean_jobs=true`
            # notifies (see config.py:solver_max_seconds_per_slice).
            "max_seconds": max_seconds if max_seconds > 0 else 5.0,
        }
        if challenge.share_target_hex:
            job["share_target"] = challenge.share_target_hex

        # Serialize daemon I/O — only one job in flight at a time
        async with self._daemon_lock:
            rec = await self._send_job_with_retry(challenge, job)

        if rec is None:
            return SolveResult(found=False, tries_used=0, elapsed_s=0.0)
        return self._result_from_record(rec, challenge)

    async def _send_job_with_retry(
        self,
        challenge: SolveChallenge,
        job: dict[str, Any],
    ) -> dict[str, Any] | None:
        # Bounded backoff so a fundamentally broken binary doesn't tight-loop:
        # ~60s of handshake timeouts × infinite retries would burn CPU and
        # spam the log. Two attempts max per slice; the outer solver loop
        # handles longer-term retry pacing.
        for attempt in (1, 2):
            try:
                daemon = await self._ensure_daemon(challenge)
                assert daemon.stdin is not None and daemon.stdout is not None
                payload = (json.dumps(job) + "\n").encode()
                daemon.stdin.write(payload)
                await daemon.stdin.drain()
                # Read one JSON line back. max_seconds + healthy slack.
                line = await asyncio.wait_for(
                    daemon.stdout.readline(),
                    timeout=max(job["max_seconds"] + 15.0, 60.0),
                )
                if not line:
                    raise ConnectionError("daemon stdout closed")
                rec = json.loads(line.decode(errors="replace").strip())
                if "error" in rec:
                    log.warning("daemon reported job error: %s", rec["error"])
                    return None
                return rec
            except (asyncio.TimeoutError, ConnectionError, json.JSONDecodeError, BrokenPipeError) as e:
                log.warning("daemon I/O failed (attempt %d): %s", attempt, e)
                await self._shutdown_daemon()
                if attempt == 2:
                    return None
                # Backoff before respawning. Linear is fine — the outer
                # solver loop handles the longer-term pacing.
                await asyncio.sleep(1.0)
        return None

    def _result_from_record(self, rec: dict[str, Any], challenge: SolveChallenge) -> SolveResult:
        found = bool(rec.get("found", False))
        tries_used = int(rec.get("tries_used", 0))
        solver_elapsed = float(rec.get("elapsed_s", 0.0))
        nonce_end_raw = rec.get("nonce64_end") or rec.get("nonce64")
        nonce_end = int(nonce_end_raw) if nonce_end_raw is not None else None

        if not found:
            return SolveResult(
                found=False,
                tries_used=tries_used,
                elapsed_s=solver_elapsed,
                raw_output=rec,
                nonce_end=nonce_end,
            )

        nonce_val = rec.get("nonce64")
        if nonce_val is None:
            nonce_val = rec.get("nonce")
        if isinstance(nonce_val, str):
            nonce_val = int(nonce_val, 16) if not nonce_val.lstrip("-").isdigit() else int(nonce_val)
        is_block = bool(rec.get("is_block", True))
        return SolveResult(
            found=True,
            tries_used=tries_used,
            elapsed_s=solver_elapsed,
            nonce=int(nonce_val) if nonce_val is not None else None,
            digest_hex=rec.get("digest") or rec.get("matmul_digest"),
            ntime=challenge.time,
            raw_output=rec,
            is_block=is_block,
        )
