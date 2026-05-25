"""Async stratum/2.0-matmul client.

Connects to a DEXBTX pool server, runs the protocol handshake, then drives a
local solver against incoming mining.notify jobs and submits found shares.

Threading model: single asyncio task with two background coroutines —
  • reader: pull JSON-RPC lines off the socket, dispatch to handlers
  • solver: pump nonce slices into btx-gbt-solve for the current job
The current job is mutated atomically by the reader; the solver re-checks
its job_id between slices and tears down on clean_jobs=true.

Reconnect: on disconnect or error, full re-handshake with exponential backoff.
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import json
import logging
import random
import time
from typing import Any

from .config import MinerConfig, fully_qualified_worker
from .gbt_solve_wrapper import (
    GbtSolveWrapper,
    SolveChallenge,
    SolveResult,
    SolverEnv,
)

log = logging.getLogger(__name__)


def _target_hex_to_compact_bits(target_hex: str) -> str:
    """Convert a 256-bit target (big-endian hex) to Bitcoin compact-bits hex.

    Mirrors src/arith_uint256.cpp::GetCompact() in Bitcoin Core: pack the most
    significant 3 bytes of the target with the byte-length as exponent, and
    handle the sign-bit-collision case by shifting one byte right.
    """
    s = target_hex.removeprefix("0x").lower()
    if len(s) % 2:
        s = "0" + s
    target_bytes = bytes.fromhex(s).rjust(32, b"\x00")[-32:]
    # Strip whole leading zero BYTES (not nibbles).
    idx = 0
    while idx < 32 and target_bytes[idx] == 0:
        idx += 1
    if idx == 32:
        return "00000000"
    significant = target_bytes[idx:]
    size = len(significant)
    if size < 3:
        mantissa = int.from_bytes(significant + b"\x00" * (3 - size), "big")
    else:
        mantissa = int.from_bytes(significant[:3], "big")
    if mantissa & 0x800000:
        mantissa >>= 8
        size += 1
    compact = (size << 24) | (mantissa & 0x00ffffff)
    return f"{compact:08x}"


@dataclasses.dataclass
class Job:
    """Parsed mining.notify payload (matmul-extended)."""
    job_id: str
    version: int
    previousblockhash: str
    merkleroot: str
    time: int
    bits: str            # network/block target
    target: str          # pool share target (looser)
    matmul: dict[str, Any]   # algorithm, seeds, n/b/r, epsilon_bits, nonce64_start
    received_at: float = dataclasses.field(default_factory=time.time)

    @classmethod
    def from_notify(cls, params: list[Any]) -> "Job":
        # Per RFC-0001: [job_id, version, prevhash, merkleroot, time, bits,
        #                share_target, clean_jobs, matmul_meta]
        return cls(
            job_id=params[0], version=int(params[1]),
            previousblockhash=params[2], merkleroot=params[3],
            time=int(params[4]), bits=params[5], target=params[6],
            matmul=params[8] if len(params) > 8 else {},
        )

    def to_solve_challenge(self) -> SolveChallenge:
        """Build a SolveChallenge for the polling-mode wrapper.

        `--bits` MUST be the block bits because nBits is hashed into the
        matmul digest (matmul_pow.cpp::ComputeMatMulHeaderHash). The pool's
        share_validator recomputes the digest using job.bits.

        For share-tier early-exit, we ALSO pass the pool's share target
        (full 256-bit BE hex) as `share_target_hex`. The patched btx-gbt-solve
        uses this to exit on share-tier hits while keeping header.nBits at
        the block bits — so the digest matches what the pool recomputes.

        Falls back gracefully on unpatched solvers: they ignore the extra
        arg and just mine at block target (solo-via-pool semantics).
        """
        compact_bits = self.bits
        share_target_hex = self.target if self.target else None

        return SolveChallenge(
            version=self.version,
            prev_hash=self.previousblockhash,
            merkle_root=self.merkleroot,
            time=self.time,
            bits=compact_bits,
            seed_a=self.matmul.get("seed_a", ""),
            seed_b=self.matmul.get("seed_b", ""),
            block_height=int(self.matmul.get("block_height", 0)),
            matmul_n=int(self.matmul.get("matmul_n", 512)),
            matmul_b=int(self.matmul.get("matmul_b", 16)),
            matmul_r=int(self.matmul.get("matmul_r", 8)),
            epsilon_bits=int(self.matmul.get("epsilon_bits", 18)),
            share_target_hex=share_target_hex,
        )


class StratumClient:
    def __init__(self, cfg: MinerConfig):
        self.cfg = cfg
        self.solver = GbtSolveWrapper(
            cfg.gbt_solve_path,
            backend=cfg.solver_backend,
            solver_threads=cfg.solver_threads,
            batch_size=cfg.solver_batch_size,
            solver_env=SolverEnv(
                # Authoritative BTX_MATMUL_* env-var names — wrong names
                # silently no-op in the solver. See docs/TUNING.md for the
                # canonical list and how to verify against your binary.
                backend=cfg.solver_backend,
                batch_size=cfg.solver_batch_size,
                prefetch_depth=cfg.solver_prefetch_depth,
                prepare_workers=cfg.solver_prepare_workers,
                pipeline_async=cfg.solver_pipeline_async,
                gpu_inputs=cfg.gpu_inputs,
                solver_threads=cfg.solver_threads,
            ),
        )
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._msg_id_seq = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._current_job: Job | None = None
        self._job_changed = asyncio.Event()
        self._extranonce1 = ""
        self._extranonce2_size = 4
        self._difficulty = 1.0
        self._solver_task: asyncio.Task | None = None
        # Stats
        self.shares_accepted = 0
        self.shares_rejected = 0
        self.blocks_found = 0

    # ── Public entry ───────────────────────────────────────────────────

    async def run_forever(self) -> None:
        backoff = self.cfg.reconnect_initial_s
        while True:
            try:
                await self._session_once()
                backoff = self.cfg.reconnect_initial_s
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("session ended: %s; reconnecting in %.1fs", e, backoff)
                await asyncio.sleep(backoff + random.uniform(0, 0.5))
                backoff = min(backoff * 2, self.cfg.reconnect_max_s)

    # ── Session lifecycle ──────────────────────────────────────────────

    async def _session_once(self) -> None:
        log.info("connecting to pool %s:%d", self.cfg.pool_host, self.cfg.pool_port)
        self._reader, self._writer = await asyncio.open_connection(
            self.cfg.pool_host, self.cfg.pool_port,
            ssl=self.cfg.pool_tls or None,
        )
        reader_task: asyncio.Task | None = None
        try:
            # Reader must run BEFORE handshake — _call awaits Futures that
            # the reader resolves, so without this we'd deadlock on subscribe.
            reader_task = asyncio.create_task(self._reader_loop())
            await self._handshake()
            self._solver_task = asyncio.create_task(self._solver_loop())
            # Reader runs until disconnect; await it for the session lifetime.
            await reader_task
        finally:
            if reader_task is not None and not reader_task.done():
                reader_task.cancel()
                try:
                    await reader_task
                except (asyncio.CancelledError, Exception):
                    pass
            if self._solver_task is not None:
                self._solver_task.cancel()
                try:
                    await self._solver_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._solver_task = None
            if self._writer is not None:
                try:
                    self._writer.close()
                    await self._writer.wait_closed()
                except Exception:
                    pass
            self._reader = self._writer = None
            self._current_job = None
            self._job_changed.set()
            self._pending.clear()

    async def _handshake(self) -> None:
        # mining.subscribe
        sub = await self._call("mining.subscribe", ["dexbtx-miner/0.1.0"])
        # Per stratum: [[[notify, sid]], extranonce1, extranonce2_size]
        try:
            self._extranonce1 = sub[1]
            self._extranonce2_size = int(sub[2])
        except (IndexError, TypeError, ValueError) as e:
            raise RuntimeError(f"bad subscribe response: {sub!r}") from e
        log.info("subscribed; extranonce1=%s en2_size=%d",
                 self._extranonce1, self._extranonce2_size)

        # mining.authorize
        worker = fully_qualified_worker(self.cfg)
        ok = await self._call("mining.authorize", [worker, ""])
        if not ok:
            raise RuntimeError(f"authorize rejected for worker={worker}")
        log.info("authorized as %s", worker)

    # ── Reader ─────────────────────────────────────────────────────────

    async def _reader_loop(self) -> None:
        assert self._reader is not None
        while True:
            line = await self._reader.readline()
            if not line:
                raise ConnectionResetError("pool closed connection")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log.warning("pool sent non-JSON: %r", line[:200])
                continue
            await self._handle_message(msg)

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        # Response to a request we made
        if "id" in msg and msg["id"] is not None and "method" not in msg:
            fut = self._pending.pop(msg["id"], None)
            if fut is not None and not fut.done():
                if msg.get("error") is not None:
                    fut.set_exception(RuntimeError(f"pool error: {msg['error']}"))
                else:
                    fut.set_result(msg.get("result"))
            return

        method = msg.get("method")
        params = msg.get("params") or []
        if method == "mining.notify":
            self._on_notify(params)
        elif method == "mining.set_difficulty":
            self._on_set_difficulty(params)
        elif method == "mining.set_extranonce":
            self._on_set_extranonce(params)
        else:
            log.debug("ignoring server message method=%s", method)

    def _on_notify(self, params: list[Any]) -> None:
        try:
            job = Job.from_notify(params)
        except (IndexError, KeyError, ValueError) as e:
            log.warning("malformed notify: %s; params=%r", e, params)
            return
        clean = bool(params[7]) if len(params) > 7 else False
        log.info("notify job=%s height_hint=prev=%s... clean=%s",
                 job.job_id, job.previousblockhash[:16], clean)
        self._current_job = job
        self._job_changed.set()

    def _on_set_difficulty(self, params: list[Any]) -> None:
        try:
            self._difficulty = float(params[0])
        except (IndexError, TypeError, ValueError):
            log.warning("bad set_difficulty params: %r", params)
            return
        log.info("difficulty set to %s", self._difficulty)

    def _on_set_extranonce(self, params: list[Any]) -> None:
        try:
            self._extranonce1 = params[0]
            self._extranonce2_size = int(params[1])
        except (IndexError, TypeError, ValueError):
            log.warning("bad set_extranonce params: %r", params)
            return
        log.info("extranonce updated en1=%s en2_size=%d",
                 self._extranonce1, self._extranonce2_size)

    # ── Solver loop ────────────────────────────────────────────────────

    async def _solver_loop(self) -> None:
        """Pump nonce slices into the solver against the current job.

        Tears down + restarts on every job change so we don't waste cycles
        on stale work.
        """
        while True:
            if self._current_job is None:
                self._job_changed.clear()
                await self._job_changed.wait()
                continue

            job = self._current_job
            nonce_start = int(job.matmul.get("nonce64_start", 0))
            log.info("solver: working job=%s nonce_start=%d slice=%d",
                     job.job_id, nonce_start, self.cfg.nonces_per_slice)

            try:
                challenge = job.to_solve_challenge()
                # Single-shot polling against upstream btx-gbt-solve.
                # Returns one SolveResult per slice (found or not-found).
                result = await self.solver.solve_slice(
                    challenge,
                    nonce_start=nonce_start,
                    max_tries=self.cfg.nonces_per_slice,
                )
                if self._current_job is None or self._current_job.job_id != job.job_id:
                    log.debug("dropping result for stale job %s", job.job_id)
                elif result.found:
                    await self._submit_share(job, result)
                # Compute the next nonce_start. Patched solver emits
                # `nonce64_end` always — that's the canonical "where to resume".
                # tries_used is an internal counter that under-counts by orders
                # of magnitude and would cause us to re-scan the same range.
                if result.nonce_end is not None and result.nonce_end > nonce_start:
                    next_nonce_start = result.nonce_end + 1
                else:
                    # Legacy/unpatched solver fallback: assume the solver
                    # scanned roughly max_tries worth of nonces.
                    next_nonce_start = nonce_start + self.cfg.nonces_per_slice
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("solver slice failed: %s", e)
                await asyncio.sleep(1.0)
                next_nonce_start = nonce_start + self.cfg.nonces_per_slice

            # If the job hasn't changed, set next nonce_start and continue.
            if self._current_job is not None and self._current_job.job_id == job.job_id:
                self._current_job.matmul["nonce64_start"] = next_nonce_start

    async def _submit_share(self, job: Job, share: SolveResult) -> None:
        worker = fully_qualified_worker(self.cfg)
        if share.nonce is None:
            log.warning("_submit_share called with no nonce; skipping")
            return
        nonce_hex = f"{share.nonce:016x}"
        ntime = f"{(share.ntime or job.time):08x}"
        extranonce2 = "00" * self._extranonce2_size
        try:
            ok = await self._call(
                "mining.submit",
                [worker, job.job_id, extranonce2, ntime, nonce_hex],
            )
        except Exception as e:
            log.warning("submit raised: %s", e)
            self.shares_rejected += 1
            return
        if ok:
            self.shares_accepted += 1
            if share.is_block:
                self.blocks_found += 1
            log.info("share OK job=%s nonce=%d (a/r/b=%d/%d/%d)",
                     job.job_id, share.nonce, self.shares_accepted,
                     self.shares_rejected, self.blocks_found)
        else:
            self.shares_rejected += 1
            log.info("share REJECTED job=%s nonce=%d (a/r=%d/%d)",
                     job.job_id, share.nonce, self.shares_accepted,
                     self.shares_rejected)

    # ── RPC helpers ────────────────────────────────────────────────────

    async def _call(self, method: str, params: list[Any]) -> Any:
        msg_id = next(self._msg_id_seq)
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        await self._send({"id": msg_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout=30.0)
        except asyncio.TimeoutError as e:
            self._pending.pop(msg_id, None)
            raise RuntimeError(f"{method} timed out") from e

    async def _send(self, msg: dict[str, Any]) -> None:
        if self._writer is None:
            raise ConnectionError("not connected")
        payload = (json.dumps(msg) + "\n").encode()
        self._writer.write(payload)
        await self._writer.drain()
