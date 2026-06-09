"""DEXBTX native stratum miner.

Speaks stratum/2.0-matmul to a DEXBTX pool server. Delegates the matmul nonce
search to a long-running `btx-gbt-solve` daemon subprocess, which holds the
canonical CUDA kernel + pre-loaded cubins for the duration of the session
(eliminating per-slice CUDA-context-init cost).
"""

__version__ = "0.4.1"
# Single source of truth for the User-Agent string sent in mining.subscribe.
# Keep this synced with pyproject.toml's [project].version on every release.
USER_AGENT = f"dexbtx-miner/{__version__}"

# Capability strings declared by this miner in `mining.subscribe`. The pool
# enforces these as a forward-compatible alternative to client-identity
# sentinels — any client (ours or third-party, e.g. easybtx) declaring the
# capability passes the gate. See RELEASE-v5.0.md §"Capability declaration".
#
# pre_hash_block_tier_v18: solver filters the early-exit pre_hash gate at
#   the block-tier target with epsilon_bits=18 (mainnet rule above height
#   nMatMulPreHashEpsilonBitsUpgradeHeight=61000). Required for v5.0+ pools.
PROTOCOL_CAPABILITIES = ["pre_hash_block_tier_v18"]
