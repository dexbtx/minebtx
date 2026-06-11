"""DEXBTX native stratum miner.

Speaks stratum/2.0-matmul to a DEXBTX pool server. Delegates the matmul nonce
search to a long-running `btx-gbt-solve` daemon subprocess, which holds the
canonical CUDA kernel + pre-loaded cubins for the duration of the session
(eliminating per-slice CUDA-context-init cost).
"""

# MUST BE BUMPED IN LOCKSTEP WITH pyproject.toml's [project].version AND with
# .solver-channel.json's "version" field. The wrapper_updater compares this
# constant against the manifest's "version" to decide whether to self-upgrade —
# if you bump pyproject.toml + channel.json but forget to bump THIS, every
# operator's wrapper_updater will infinite-loop installing the same tarball
# (it never sees __version__ catch up). See v0.4.8 CHANGELOG for the incident.
__version__ = "0.4.9"
# Single source of truth for the User-Agent string sent in mining.subscribe.
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
