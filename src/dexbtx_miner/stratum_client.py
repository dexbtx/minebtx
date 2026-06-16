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
import uuid as _uuid
from typing import Any

from . import __version__ as _wrapper_version

from . import canonical_names, hardware
from .config import MinerConfig, fully_qualified_worker
from .gbt_solve_wrapper import (
    GbtSolveWrapper,
    SolveChallenge,
    SolveResult,
    SolverEnv,
)

# Period for `worker.report_metrics` heartbeats. 60s matches the spec in
# RELEASE-v5.0.md §2c; tune via env if needed.
METRICS_REPORT_INTERVAL_SEC = 60.0

log = logging.getLogger(__name__)


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
        """Build a SolveChallenge for the daemon-mode wrapper.

        `--bits` MUST be the block bits because nBits is hashed into the
        matmul digest (matmul_pow.cpp::ComputeMatMulHeaderHash). The pool's
        share_validator recomputes the digest using job.bits.

        For share-tier early-exit, we ALSO pass the pool's share target
        (full 256-bit BE hex) as `share_target_hex`. The patched btx-gbt-solve
        uses this to exit on share-tier hits while keeping header.nBits at
        the block bits — so the digest matches what the pool recomputes.

        Raises `ValueError` if the pool omits the matmul metadata fields
        the solver needs — failing loud beats silently mining for height 0
        with all-zero seeds (which produces 100% rejected shares with no
        clear diagnostic).
        """
        compact_bits = self.bits
        share_target_hex = self.target if self.target else None

        # Fail loud on missing matmul metadata. The pool MUST send these in
        # every notify; if it doesn't, we shouldn't silently invent values
        # (silently mining at height 0 with empty seeds = 100% rejects).
        required_matmul_keys = ("seed_a", "seed_b", "block_height")
        missing = [k for k in required_matmul_keys if k not in self.matmul]
        if missing:
            raise ValueError(
                f"notify missing required matmul fields: {missing} "
                f"(got keys: {sorted(self.matmul.keys())}); "
                f"refusing to mine with placeholder values"
            )

        return SolveChallenge(
            version=self.version,
            prev_hash=self.previousblockhash,
            merkle_root=self.merkleroot,
            time=self.time,
            bits=compact_bits,
            seed_a=self.matmul["seed_a"],
            seed_b=self.matmul["seed_b"],
            block_height=int(self.matmul["block_height"]),
            matmul_n=int(self.matmul.get("matmul_n", 512)),
            matmul_b=int(self.matmul.get("matmul_b", 16)),
            matmul_r=int(self.matmul.get("matmul_r", 8)),
            epsilon_bits=int(self.matmul.get("epsilon_bits", 18)),
            share_target_hex=share_target_hex,
            # V3 (height >= 130,500): parent block median-time-past. Optional and
            # additive — absent from pre-v3 pools; harmless below activation since
            # the solver ignores it until 130,500. NOT in required_matmul_keys.
            parent_mtp=(int(self.matmul["parent_mtp"]) if "parent_mtp" in self.matmul else None),
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
        # H2/H3 cohort fields — computed once at session create and cached.
        # The pool's /api/fleet groups workers by these (canary vs stable A/B).
        # solver_sha256 changes only when the auto-updater swaps the binary
        # (which forces a process restart), so caching is safe.
        # NOTE: read __version__ directly from the package, not via
        # importlib.metadata.version() — the metadata lookup picked up a
        # stale dist-info from an unrelated install during v0.4.8 testing
        # and reported '0.3.0' on a rig running 0.4.8. The constant is the
        # only canonical source.
        self._wrapper_version = _wrapper_version
        self._solver_sha256 = hardware.solver_sha256_hex(cfg.gbt_solve_path)
        self._solver_backend = cfg.solver_backend
        self._difficulty = 1.0
        self._solver_task: asyncio.Task | None = None
        self._metrics_task: asyncio.Task | None = None
        self._heal_task: asyncio.Task | None = None
        # ── v0.4.18 solver auto-heal state (see config.heal_*) ──────────────
        # All monotonic() timestamps; reset on every (re)connect so a pool
        # restart's reconnect starts a fresh window and can't self-trip.
        self._session_started_at = time.monotonic()
        self._last_accept_at = time.monotonic()
        self._last_solver_result_at = time.monotonic()
        self._consec_rejects = 0          # trigger B: rejects since last accept
        self._last_heal_at = 0.0
        self._heals_without_accept = 0    # escalates cooldown if heals don't help
        # v5.0: session_id is a local UUID echoed in periodic metrics so the
        # pool can correlate `worker.report_metrics` to a single connection
        # even if a worker reconnects mid-window.
        self._session_id: str = ""
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
            # v0.4.18 — fresh auto-heal window per session: a reconnect (e.g.
            # after a pool restart) must not count pre-reconnect silence/rejects.
            _now = time.monotonic()
            self._session_started_at = _now
            self._last_accept_at = _now
            self._last_solver_result_at = _now
            self._consec_rejects = 0
            self._solver_task = asyncio.create_task(self._solver_loop())
            self._metrics_task = asyncio.create_task(self._metrics_loop())
            if self.cfg.heal_enabled:
                self._heal_task = asyncio.create_task(self._heal_loop())
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
            if self._metrics_task is not None:
                self._metrics_task.cancel()
                try:
                    await self._metrics_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._metrics_task = None
            if self._heal_task is not None:
                self._heal_task.cancel()
                try:
                    await self._heal_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._heal_task = None
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
        # mining.subscribe — v5.0 carries TWO new params in the trailing
        # dict: `protocol_compliant` (capability array) and `hardware`
        # (one-shot static fingerprint). Older pools that don't understand
        # the extra params still see params[0] (user agent) and ignore
        # the rest. v5.0+ pools reject if `pre_hash_block_tier_v18` is
        # missing from `protocol_compliant`.
        from . import PROTOCOL_CAPABILITIES, USER_AGENT, __version__
        self._session_id = _uuid.uuid4().hex
        # v0.3.2 — solver_env lets the pool give data-backed tuning
        # recommendations. Mirrors the SolverEnv constructed for the
        # solver wrapper above; canonical BTX_MATMUL_* names.
        solver_env: dict[str, Any] = {
            "BTX_MATMUL_BACKEND": self.cfg.solver_backend,
            "BTX_MATMUL_GPU_INPUTS": self.cfg.gpu_inputs,
            "BTX_MATMUL_SOLVE_BATCH_SIZE": self.cfg.solver_batch_size,
            "BTX_MATMUL_PREPARE_PREFETCH_DEPTH": self.cfg.solver_prefetch_depth,
            "BTX_MATMUL_PREPARE_WORKERS": self.cfg.solver_prepare_workers,
            "BTX_MATMUL_PIPELINE_ASYNC": self.cfg.solver_pipeline_async,
            "BTX_MATMUL_SOLVER_THREADS": self.cfg.solver_threads,
        }
        # v0.3.3 — also forward any BTX_MATMUL_* env vars present in the
        # operator's shell that AREN'T already covered by the canonical
        # set above. Lets the pool capture custom solver patches (e.g.
        # BTX_MATMUL_CUDA_POOL_SLOTS) without requiring a matching miner
        # release. Cfg-derived values take precedence (we only add keys
        # NOT already present) since cfg drives what gbt-solve actually
        # receives at runtime.
        import os
        for k, v in os.environ.items():
            if k.startswith("BTX_MATMUL_") and k not in solver_env:
                solver_env[k] = v
        hw = hardware.collect_static_hardware(
            miner_version=__version__,
            cpu_threads_allocated=self.cfg.solver_threads,
            solver_env=solver_env,
            solver_path=self.cfg.gbt_solve_path,
        )
        log.info("hardware: %s", hardware.hardware_summary_string(hw))
        extension = {
            "protocol_compliant": list(PROTOCOL_CAPABILITIES),
            "hardware": hw,
            "session_id": self._session_id,
        }
        sub = await self._call("mining.subscribe", [USER_AGENT, extension])
        # Per stratum: [[[notify, sid]], extranonce1, extranonce2_size]
        try:
            self._extranonce1 = sub[1]
            self._extranonce2_size = int(sub[2])
        except (IndexError, TypeError, ValueError) as e:
            raise RuntimeError(f"bad subscribe response: {sub!r}") from e
        log.info("subscribed; extranonce1=%s en2_size=%d session=%s",
                 self._extranonce1, self._extranonce2_size, self._session_id[:8])

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
        elif method == "mining.set_canonical_name":
            self._on_set_canonical_name(params)
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
        # v0.4.2 — on clean=false (mempool/coinbase update within the
        # same parent block), CARRY FORWARD the solver's nonce-progress
        # from the previous job. The pool's broadcast `nonce64_start`
        # is fixed per-session (extranonce1<<32 | base_offset). Without
        # this carry-over the solver restarts at the same nonce on every
        # ~5s template rebuild and never advances. On clean=true (new
        # parent block) we keep the broadcast value — that's a real
        # reset signal. Throughput-zero bug verified on M4: 6 consecutive
        # clean=false notifies all left nonce_start=704374636544.
        if not clean and self._current_job is not None:
            prev_start = self._current_job.matmul.get("nonce64_start")
            if prev_start is not None:
                job.matmul["nonce64_start"] = prev_start
        self._current_job = job
        self._job_changed.set()

        # Tip-change preempt: if the solver is mid-slice on a DIFFERENT parent
        # block, abort that slice now (SIGUSR1) instead of letting it burn up to
        # max_seconds on a dead tip (wasted hashrate + stale shares). Same-parent
        # rotations (coinbase/mempool churn, clean=False) are NOT preempted — that
        # work is still valid against the new merkle root.
        inflight = getattr(self, "_inflight_parent", None)
        if inflight is not None and inflight != job.previousblockhash:
            try:
                if self.solver.preempt():
                    log.info("tip change %s -> %s: preempting in-flight slice",
                             inflight[:16], job.previousblockhash[:16])
            except Exception as e:
                log.debug("preempt signal failed: %s", e)

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

    def _on_set_canonical_name(self, params: list[Any]) -> None:
        """Handle pool→miner canonical name assignment (one per GPU).

        The pool sends one `mining.set_canonical_name` notification per
        GPU after the first hardware report. We accept either a single
        params object or a list (some implementations may batch).
        """
        items: list[dict[str, Any]] = []
        if isinstance(params, dict):
            items = [params]
        elif isinstance(params, list):
            for p in params:
                if isinstance(p, dict):
                    items.append(p)
        if not items:
            log.warning("set_canonical_name: empty/malformed params %r", params)
            return
        for item in items:
            uuid_str = item.get("gpu_uuid")
            name = item.get("canonical_name")
            if not (uuid_str and name):
                log.warning("set_canonical_name: missing required field; got %r", item)
                continue
            canonical_names.upsert(
                gpu_uuid=uuid_str,
                canonical_name=name,
                operator_label=item.get("operator_label"),
                assigned_at=int(item.get("assigned_at", time.time())),
            )
        banner = canonical_names.format_assignment_banner(items)
        if banner:
            # Print to stdout for unmissable operator visibility, and also
            # log at INFO so rotating log files capture it.
            print(banner, flush=True)
            for line in banner.splitlines():
                log.info("%s", line)

    # ── Solver loop ────────────────────────────────────────────────────

    async def _solver_loop(self) -> None:
        """Pump nonce slices into the solver against the current job.

        v0.4.3 — slice-result handling rewritten from first principles.
        Old (pre-v0.4.3) flow dropped any slice result whose job_id no
        longer matched `_current_job.job_id`. With BTX's pool emitting
        a notify every ~5s on mempool churn (same parent block,
        `clean=False`) and slice durations of ~5–15s, EVERY slice
        intersected with at least one notify. Result: every slice's
        work was dropped, nonce_start never advanced, throughput ≈ 0.

        The job_id check was misplaced: stratum allows submission
        against any cached job_id, and the pool keeps a JobCache (8192
        slots since v0.8.6) precisely to serve same-parent rotations.
        New flow:
          - ALWAYS submit found shares — pool's validator + JobCache
            handle staleness correctly; the wrapper has no business
            second-guessing.
          - ALWAYS advance nonce_start across same-parent rotations
            (use `previousblockhash` equality, NOT `job_id`). A
            different job_id with the same parent is just a coinbase /
            merkle rotation; our nonce-scan position is still valid.
          - On real parent change (`previousblockhash` differs) the new
            job's broadcast `nonce64_start` is honored (true reset).
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
                # Record the parent this slice is solving so _on_notify can
                # SIGUSR1-preempt it if a NEW block (different parent) arrives.
                self._inflight_parent = job.previousblockhash
                # Single-shot polling against upstream btx-gbt-solve.
                # Returns one SolveResult per slice (found or not-found).
                result = await self.solver.solve_slice(
                    challenge,
                    nonce_start=nonce_start,
                    max_tries=self.cfg.nonces_per_slice,
                    max_seconds=self.cfg.solver_max_seconds_per_slice,
                )
                self._inflight_parent = None
                # v0.4.18 heal: any return (found or not) proves the solver is
                # alive — clears the hang (trigger A) timer.
                self._last_solver_result_at = time.monotonic()
                # v0.4.3 — always submit found shares; let pool decide
                # validity via its JobCache. Pre-v0.4.3 this was gated on
                # current_job.job_id matching slice.job_id and dropped
                # every slice that intersected a notify (~all of them at
                # 5s notify cadence + 5–15s slice duration).
                # Stale-parent guard: if the tip moved to a DIFFERENT parent
                # while this slice ran (e.g. a preempt fired but the slice had
                # already found a share on the old parent), those shares are
                # stale — the pool would reject them (wrong previousblockhash;
                # JobCache only forgives same-parent rotations). Drop them.
                _cur = self._current_job
                _stale = _cur is not None and _cur.previousblockhash != job.previousblockhash
                if result.found and _stale:
                    log.info(
                        "dropping stale share(s): slice parent %s != current %s (tip moved mid-slice)",
                        job.previousblockhash[:16], _cur.previousblockhash[:16],
                    )
                if result.found and not _stale:
                    if self._current_job is None or self._current_job.job_id != job.job_id:
                        log.info(
                            "submitting found share for rotated job_id %s "
                            "(current=%s): pool's JobCache handles same-parent staleness",
                            job.job_id,
                            self._current_job.job_id if self._current_job else "(none)",
                        )
                    # C4: the solver now returns ALL shares found in the slice
                    # (raw_output["shares"]); submit each so none are wasted.
                    _shs = (result.raw_output or {}).get("shares")
                    if _shs:
                        for _s in _shs:
                            try:
                                _n = int(_s.get("nonce64"))
                            except (TypeError, ValueError):
                                continue
                            # Fire submissions in the background so the daemon
                            # is not starved while we submit a slice-worth of
                            # shares. Keep a ref so the task is not GC'd.
                            _st = getattr(self, "_submit_tasks", None)
                            if _st is None:
                                _st = set()
                                self._submit_tasks = _st
                            _t = asyncio.create_task(self._submit_share(job, SolveResult(
                                found=True, tries_used=0, elapsed_s=0.0,
                                nonce=_n, digest_hex=_s.get("matmul_digest"),
                                ntime=result.ntime, raw_output=_s,
                                is_block=bool(_s.get("is_block", False)),
                            )))
                            _st.add(_t)
                            _t.add_done_callback(_st.discard)
                    else:
                        await self._submit_share(job, result)
                # Compute the next nonce_start. Patched solver emits
                # `nonce64_end` always — canonical "where to resume".
                # tries_used under-counts by orders of magnitude and
                # would cause re-scanning the same range.
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

            # v0.4.3 — advance nonce_start across same-parent rotations,
            # not just same-job_id. With pool sending clean=False
            # rotations every ~5s (mempool/coinbase rebuild on the same
            # parent), our nonce-scan position is still valid against
            # the new merkle root — different coinbase, same parent
            # block, same V2 nonce-seed domain. Gating on job_id meant
            # nonce_start was reset to the broadcast value on every
            # rotation (because the new job_id never matches the slice's
            # old job_id under 5s notify cadence + 5–15s slice duration).
            #
            # On real parent change (clean=True), the new job's
            # `previousblockhash` differs and we correctly leave its
            # broadcast `nonce64_start` in place (true reset).
            if (
                self._current_job is not None
                and self._current_job.previousblockhash == job.previousblockhash
            ):
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
            self._consec_rejects += 1  # v0.4.18 heal: count toward desync trigger
            return
        if ok:
            self.shares_accepted += 1
            if share.is_block:
                self.blocks_found += 1
            # v0.4.18 heal: an accept clears the desync streak + heal backoff.
            self._consec_rejects = 0
            self._last_accept_at = time.monotonic()
            self._heals_without_accept = 0
            log.info("share OK job=%s nonce=%d (a/r/b=%d/%d/%d)",
                     job.job_id, share.nonce, self.shares_accepted,
                     self.shares_rejected, self.blocks_found)
        else:
            self.shares_rejected += 1
            self._consec_rejects += 1  # v0.4.18 heal: count toward desync trigger
            log.info("share REJECTED job=%s nonce=%d (a/r=%d/%d)",
                     job.job_id, share.nonce, self.shares_accepted,
                     self.shares_rejected)

    # ── v0.4.18 solver auto-heal watchdog ──────────────────────────────
    async def _heal_loop(self) -> None:
        """Bounce the solver daemon when it wedges (see config.heal_*).

        Two work-proportional triggers, neither a naive wall-clock-since-accept
        (which would punish slow GPUs / high vardiff / cold starts):
          B (wrong-digest): >= heal_consec_rejects submitted-and-rejected
            shares with zero accepts since the last accept. Self-resets on any
            accept, so a healthy rig never accumulates.
          A (hang): no solver result for heal_solver_stall_secs while the
            process is alive (after a cold-start grace) — the home-1070 case,
            where a/r/b freezes and B can never fire.

        Recovery is cheap (~one daemon respawn) vs the wedge (100% loss), so we
        bias toward bouncing. Cooldown escalates while heals don't yield an
        accept, preventing a restart loop; it resets the instant one lands.
        """
        while True:
            await asyncio.sleep(self.cfg.heal_check_interval_secs)
            now = time.monotonic()
            # Escalating cooldown: base × (1 + heals_without_accept), capped ×6.
            cooldown = self.cfg.heal_cooldown_secs * (
                1 + min(self._heals_without_accept, 5)
            )
            if now - self._last_heal_at < cooldown:
                continue

            reason: str | None = None
            if self._consec_rejects >= self.cfg.heal_consec_rejects:
                reason = (
                    f"desync: {self._consec_rejects} consecutive rejects, "
                    f"0 accepts (trigger B)"
                )
            elif (
                now - self._last_solver_result_at > self.cfg.heal_solver_stall_secs
                and now - self._session_started_at > self.cfg.heal_first_slice_grace_secs
            ):
                reason = (
                    f"hang: no solver result for "
                    f"{now - self._last_solver_result_at:.0f}s (trigger A)"
                )
            if reason is None:
                continue

            log.warning(
                "AUTO-HEAL: bouncing solver daemon — %s "
                "(a=%d r=%d heals_since_accept=%d)",
                reason, self.shares_accepted, self.shares_rejected,
                self._heals_without_accept,
            )
            try:
                await self.solver.force_restart()
            except Exception as e:
                log.warning("auto-heal restart failed: %s", e)
            self._last_heal_at = now
            self._heals_without_accept += 1
            # Clear the trigger state so we wait a full window before re-firing;
            # the next genuine accept resets _heals_without_accept (and cooldown).
            self._consec_rejects = 0
            self._last_solver_result_at = now

    # ── Periodic metrics ───────────────────────────────────────────────

    async def _metrics_loop(self) -> None:
        """Send `worker.report_metrics` every ~60s for the session lifetime.

        Failures are logged but don't tear down the session — periodic
        telemetry is best-effort. The pool's `worker.report_metrics`
        handler returns a simple ack; if it doesn't recognize the method
        (older pool), the call errors and we just continue.
        """
        # Initial offset: stagger across miners so the pool doesn't get
        # synchronized spikes from a large fleet upgrading at the same time.
        await asyncio.sleep(random.uniform(5.0, METRICS_REPORT_INTERVAL_SEC))
        loop = asyncio.get_running_loop()
        while True:
            try:
                solver_nps = getattr(self.solver, "last_observed_nps", None)
                # collect_runtime_metrics multi-samples nvidia-smi over ~2 s.
                # Running it inline would block the asyncio loop for that
                # window — which stalls solver-stdout reads and lets the
                # solver pipe-fill and pause, making the GPU appear idle
                # *precisely while we're trying to measure it*. Always run
                # in a thread so the event loop keeps draining solver I/O.
                payload = await loop.run_in_executor(
                    None,
                    lambda: hardware.collect_runtime_metrics(
                        session_id=self._session_id,
                        solver_nps=solver_nps,
                        shares_session_total=self.shares_accepted + self.shares_rejected,
                        wrapper_version=self._wrapper_version,
                        solver_sha256=self._solver_sha256,
                        solver_backend=self._solver_backend,
                    ),
                )
                # Notify-style call: don't await a result (some pools reply,
                # some don't; we don't care for periodic telemetry).
                await self._send({
                    "method": "worker.report_metrics",
                    "params": [payload],
                })
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("metrics report failed (non-fatal): %s", e)
            await asyncio.sleep(METRICS_REPORT_INTERVAL_SEC)

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
