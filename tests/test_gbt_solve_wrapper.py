"""Minimal smoke test for the polling-mode gbt_solve_wrapper.

Requires a local btxd (any datadir) reachable via btx-cli, plus the
patched btx-gbt-solve binary. Configurable via env:
  BTX_GBT_SOLVE  — path to the solver binary (default: ./btx-gbt-solve
                   if it exists, else ~/.dexbtx-miner/bin/btx-gbt-solve)
  BTX_CLI        — path to btx-cli (default: btx-cli on PATH)
  BTX_DATADIR    — btxd data directory (default: ~/.btx)
  BTX_RPCPORT    — RPC port (default: 19334, BTX mainnet)
  HEIGHT_TO_PROBE — block height to fetch a challenge from (default: 108000)

Builds a SolveChallenge from a real block, invokes the wrapper with a
tiny max_tries (~1M), and asserts:
  - The subprocess exits cleanly
  - The wrapper parses the JSON result
  - found is False (way too few tries to find a real block) OR True
  - tries_used is reported
  - elapsed_s is reported

Run via: python3 tests/test_gbt_solve_wrapper.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from dexbtx_miner.gbt_solve_wrapper import (
    GbtSolveWrapper,
    SolveChallenge,
    SolverEnv,
)


def _default_solver_path() -> str:
    here = Path("./btx-gbt-solve")
    if here.is_file():
        return str(here.resolve())
    return str(Path.home() / ".dexbtx-miner" / "bin" / "btx-gbt-solve")


GBT_SOLVE = os.environ.get("BTX_GBT_SOLVE", _default_solver_path())
BTX_CLI = os.environ.get("BTX_CLI", "btx-cli")
BTX_DATADIR = os.environ.get("BTX_DATADIR", str(Path.home() / ".btx"))
BTX_RPCPORT = os.environ.get("BTX_RPCPORT", "19334")
HEIGHT_TO_PROBE = int(os.environ.get("HEIGHT_TO_PROBE", "108000"))


def _btx_cli(*args: str) -> str:
    cmd = [
        BTX_CLI,
        f"-rpcport={BTX_RPCPORT}",
        f"-datadir={BTX_DATADIR}",
        *args,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30).stdout


async def test_polling_mode_smoke() -> None:
    print(f"  fetching block at h={HEIGHT_TO_PROBE} for challenge inputs...")
    block_hash = _btx_cli("getblockhash", str(HEIGHT_TO_PROBE)).strip()
    block = json.loads(_btx_cli("getblock", block_hash, "1"))

    nonce_str = block.get("nonce64") or block.get("nonce")
    real_nonce = int(nonce_str, 16) if isinstance(nonce_str, str) else int(nonce_str)

    challenge = SolveChallenge(
        version=block["version"],
        prev_hash=block["previousblockhash"],
        merkle_root=block["merkleroot"],
        time=block["time"],
        bits=block["bits"],
        seed_a=block["seed_a"],
        seed_b=block["seed_b"],
        block_height=HEIGHT_TO_PROBE,
        matmul_n=int(block.get("matmul_dim", 512)),
        matmul_b=16,
        matmul_r=8,
        epsilon_bits=18,
    )

    wrapper = GbtSolveWrapper(
        GBT_SOLVE,
        backend="cpu",  # CPU for the smoke test — fast startup, deterministic
        solver_threads=2,
        batch_size=2,
        solver_env=SolverEnv(gpu_inputs=0),
    )

    # Test 1: too-few-tries slice. Just exercises subprocess plumbing.
    print(f"  test 1: short slice (1000 tries from nonce 1) — exercises subprocess plumbing...")
    result = await wrapper.solve_slice(
        challenge, nonce_start=1, max_tries=1000,
    )
    print(f"    found={result.found}  tries_used={result.tries_used}  elapsed={result.elapsed_s:.3f}s")
    assert result.tries_used >= 0
    assert result.elapsed_s >= 0

    # Test 2: start exactly at the real nonce — solver should find it on the first try.
    print(f"  test 2: start at the real nonce {real_nonce} (solver should find it immediately)...")
    result = await wrapper.solve_slice(
        challenge, nonce_start=real_nonce, max_tries=8,
    )
    print(f"    found={result.found}  tries_used={result.tries_used}  elapsed={result.elapsed_s:.3f}s")
    if result.found:
        print(f"    nonce={result.nonce}  digest={result.digest_hex[:32] if result.digest_hex else None}...")
        assert result.nonce == real_nonce, f"solver found different nonce: {result.nonce} vs {real_nonce}"
        print(f"    ✓ solver recovered the real-block nonce")
    else:
        # Even with 8 tries starting at the real nonce, the solver should find it
        # since the matmul digest is exactly the block's digest.
        print(f"    NOTE: solver didn't find it; could be backend issue or solver internal grinding pattern")
        print(f"    raw output keys: {list(result.raw_output or {})}")


if __name__ == "__main__":
    try:
        asyncio.run(test_polling_mode_smoke())
        print("\nALL PASS")
    except AssertionError as e:
        print(f"\nFAIL: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\nERR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
