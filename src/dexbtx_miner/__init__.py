"""DEXBTX native stratum miner.

Speaks stratum/2.0-matmul to a DEXBTX pool server. Delegates the matmul nonce
search to a long-running `btx-gbt-solve` daemon subprocess, which holds the
canonical CUDA kernel + pre-loaded cubins for the duration of the session
(eliminating per-slice CUDA-context-init cost).
"""

__version__ = "0.1.0-dev"
