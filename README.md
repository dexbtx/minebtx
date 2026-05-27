# dexbtx-miner

The reference mining client for the [DEXBTX](https://minebtx.com) pool — a
PPLNS pool for [BTX](https://btxprice.com), the post-quantum Bitcoin Knots
fork with matmul proof-of-work.

- **Pool stratum**: `stratum+tcp://stratum.minebtx.com:3333`
- **Live dashboard**: https://pool.minebtx.com
- **Telegram bot**: [@btxdexbot](https://t.me/btxdexbot) — `/stats`, `/mybalance`, `/blocks_today`
- **Pool fee**: 2.5%, weekly batched payouts, no minimum threshold

---

## Why pool with DEXBTX

**Smoother earnings vs. solo variance.** At BTX's current network hashrate
a single 5070-class card finds a block roughly every 4-7 days *on average*
solo — but variance is high; you can go 3 weeks without one. PPLNS pays
you continuously based on the shares you submit, so income tracks your
actual hashrate rather than your luck. The pool eats the variance.

**Block propagation that actually wins races.** BTX's P2P mesh is small
and slow — every minute we tested, our orphan rate ran 30-40% on raw
internet routing. The pool runs a dedicated **block-forwarder** (with
ancestor push) that pushes wins straight into the highest-connectivity
peers on the network the instant the solver returns. We dropped fleet
orphan rate from ~38% to under 5% with this single piece of plumbing.
You inherit that propagation just by pointing your miner at us.

**Peer management we already did the legwork on.** Our pool's btxd node
maintains a curated mesh of regional peers across NA / EU / SEA / AU.
Solo miners spend the first week hunting for non-stalling peers — we
hand-tuned ours and route block templates through them automatically.

**Built-in fallbacks for the things that actually break.** A year of
running this fleet uncovered roughly a dozen silent-failure modes —
shielded-state corruption after reorg storms, btxd hanging on slow
peer block-body delivery, tailscaled crashes in userspace containers,
CUDA buffer pool exhaustion at batch=256. The miner ships with reconnect
backoff, daemon respawn, and a hard CUDA-engagement smoke test in the
installer. You won't silently mine on CPU for three weeks like we did.

**Simplicity.** Solo mining requires running your own `btxd` (full node,
600 GB chainstate, manual peer wrangling, manual restart after every
reorg storm). DEXBTX mining is `curl | bash | mine`. One binary, one
YAML, one tmux session. No node admin.

**Pool-side transparency.** 2.5% fee, batched payouts every Friday
18:00 UTC, full payout transactions DM'd to you via Telegram. No
minimum-payout threshold — you get paid for every share that lands in
the 10K-share PPLNS window regardless of size.

---

## Quick start (one line)

Linux + any NVIDIA GPU from Pascal (GTX 10-series) through Blackwell (RTX 50-series):

```bash
curl -fsSL https://minebtx.com/install.sh | bash -s -- --address 'btx1z…YOUR_BTX_ADDRESS…'
```

What this does:

1. **Detects your GPU** and writes a tuned `~/.dexbtx-miner/config.yaml`
   with per-card workers / threads / batch size (measured profiles for
   all major NVIDIA gens — see [docs/TUNING.md](docs/TUNING.md))
2. **Downloads the `btx-gbt-solve` solver binary** from the GitHub
   release for your chosen `PREBUILDS_TAG`, SHA256-verifies it against
   the value pinned in [`pyproject.toml`](pyproject.toml), and refuses
   to install on mismatch
3. **Runs a hard GPU-acceleration smoke test** that *fails loudly* if
   CUDA didn't actually engage — catches the silent-CPU-fallback class
   of bug that would otherwise mine at 1/1000th the throughput without
   you ever noticing
4. **Installs the `dexbtx-miner` Python orchestrator** (this repo) via
   `pip install --user`

When the install finishes:

```bash
dexbtx-miner --config ~/.dexbtx-miner/config.yaml
# or, persistent in tmux:
tmux new -d -s dexbtx 'dexbtx-miner --config ~/.dexbtx-miner/config.yaml 2>&1 | tee -a ~/.dexbtx-miner/miner.log'
```

Within a minute you should see `share OK` lines in the log and your
worker should appear on the [live dashboard](https://pool.minebtx.com).

### Get DM'd on weekly payouts

Pick a `worker_name` in your config (default: your hostname). Then on Telegram:

```
/link <your_worker_name>
```

The bot binds your TG chat to that worker and DMs you the payout transaction
every Friday 18:00 UTC. Other bot commands: `/stats`, `/mybalance <name>`,
`/blocks_today`, `/myblock <name>`, `/help`.

**Recommended naming pattern:** `<alias>-<gpu>-<country2>-<color>`
(four dash-separated parts, all lowercase) — e.g. `kappa-5060ti-us-blue`,
`bravo-1070-de-red`, `tango-4060ti-ca-green`.

Parts:
- `<alias>` — your handle for this rig (any nickname; avoid your real name)
- `<gpu>` — short GPU model (`1070`, `2080ti`, `3060ti`, `4060ti`, `5070`, …)
- `<country2>` — ISO 3166-1 alpha-2 country code (`us`, `de`, `ca`, `gb`, `jp`, …)
- `<color>` — short, unique-to-you suffix to disambiguate multiple identical rigs

Why the country + color suffix? Two miners both running 5060 Tis in the US
would otherwise collide on `home-5060ti`. The country + color tail keeps
your worker rows unique on the dashboard and routable through the bot's
`/link` command without ambiguity.

Stick with one name per rig once chosen — renaming creates a new worker
row and earned shares stay with the old name.

---

## What's in this repo

```
.
├── install.sh                    # one-line installer (curl | bash entry point)
├── pyproject.toml                # Python package definition + pinned solver SHA256
├── config.example.yaml           # config template (copy + edit address)
├── src/dexbtx_miner/             # the Python client
│   ├── __main__.py               # CLI entry: `dexbtx-miner --config <yaml>`
│   ├── stratum_client.py         # stratum/2.0-matmul protocol client
│   ├── gbt_solve_wrapper.py      # async daemon-mode wrapper around btx-gbt-solve
│   ├── benchmark.py              # `dexbtx-miner benchmark` — sweep + tune
│   └── config.py                 # YAML config loader + SolverEnv plumbing
├── tests/
│   └── test_gbt_solve_wrapper.py # smoke test for the wrapper
├── docs/
│   ├── ONBOARDING.md             # fresh starters / converters / self-hosters — pick your path
│   └── TUNING.md                 # per-GPU env-var reference + sweep methodology
└── release-tooling/
    ├── README.md                 # about the binary release assets
    ├── SHA256SUMS                # hashes pinned to current release tag
    └── publish-release.sh        # `gh release create` helper for new versions
```

The `btx-gbt-solve` binary itself isn't committed to the repo — it's
published as a [GitHub Release asset](../../releases/latest). The
installer fetches it and SHA-verifies against `pyproject.toml`.

---

## Per-GPU tuning profile (measured)

The installer auto-picks these based on the detected GPU. Every entry
in this table was measured on real hardware via the
`dexbtx-miner benchmark` sweep methodology in [docs/TUNING.md](docs/TUNING.md).

| GPU | workers | threads | batch | Measured util / power |
|---|---|---|---|---|
| GTX 1070 (Pascal sm_61) | 16 | 8 | 128 | 83% / 113W (memory-bound — Pascal's ceiling) |
| RTX 2080 Ti (Turing sm_75) | 12 | 8 | 128 | 100% / 296W |
| RTX 3060 Ti (Ampere sm_86) | 12 | 8 | 128 | 100% / 190W |
| RTX 4060 Ti (Ada Lovelace sm_89) | 12 | 8 | 128 | 100% / 164W |
| RTX 5060 Ti (Blackwell sm_120) | 16 | 8 | 128 | 99% / 150W |
| RTX 5070 (Blackwell sm_120) | 16 | 8 | 128 | 100% / 223W |
| RTX 5070 Ti (Blackwell sm_120) | 16 | 8 | 128 | (avoid batch=256 — broke CUDA on this card historically) |

**Universal default**: `workers=16 threads=8 batch=128 prefetch=8`
works on every NVIDIA generation from Pascal through Blackwell. The
installer writes this for any unknown card.

The dominant levers are `solver_prepare_workers` + `solver_threads`,
working together — the matmul backend is CPU-input-prep-bound on every
modern card we've tested. Raising the pair from `workers=8/threads=4`
to `workers=16/threads=8` took an RTX 5070 from 70% → 100% util. Bump
them as a unit; they go hand in hand. Batch size has no effect on
steady-state throughput once prep is sized correctly.

---

## Architecture coverage (which GPUs work)

The shipped `btx-gbt-solve` binary (`v4.4-sm75-sm86`) embeds native
cubins for **six** architectures — every dominant consumer GPU runs
natively on its own arch, no PTX-JIT required:

| Card | Path |
|---|---|
| GTX 10-series / Pascal sm_61 | Native sm_61 cubin |
| RTX 20-series / Turing sm_75 | Native sm_75 cubin |
| RTX 30-series / Ampere consumer sm_86 | Native sm_86 cubin |
| RTX 40-series / Ada sm_89 | Native sm_89 cubin |
| Hopper sm_90 (H100, H200) | Native sm_90 cubin |
| RTX 50-series / Blackwell sm_120 | Native sm_120 cubin |

This closes the CUDA 13 + Ampere/Turing silent-CPU-fallback gap that
older releases hit: v0.2.2 and earlier shipped only sm_61/89/90/120
native and relied on the sm_61 PTX section to JIT-compile to sm_75 /
sm_86 at driver load. CUDA 13 deprecated compute_61 as a JIT source,
breaking that path for every 20- and 30-series miner on a current
driver. From v0.2.5 onward the missing arches ship as native cubins,
no JIT dependency.

**If you tried v0.2.4 on an RTX 20-series or 30-series and saw silent
CPU fallback:** that was the temporary state after the v0.2.3 rollback
to v4.3. Re-run `install.sh` to pull the v4.4 binary and the GPU will
re-engage at full utilization. Never downgrade your driver.

---

## Common pitfalls (we hit these so you don't have to)

### GPU util sustains below 95%

CPU-side input prep can't keep the matmul kernel fed. Bump
`solver_prepare_workers` to 16 (and `solver_threads` to 8) in your
config. **Batch size, prefetch depth, and pipeline_async are NOT the
dominant levers** — workers and threads are.

### Only "share REJECTED" with codes 21, 22, 23

Right after a stratum reconnect (or after the pool restarts), your
miner replays old work — that produces:
- code 21 (`job stale`) — slice ran past the 180s freshness window
- code 22 (`duplicate share`) — same nonce already submitted
- code 23 (`digest >= share_target`) — pool difficulty changed during reconnect

All three are normal post-reconnect noise. Wait 1–2 minutes; new jobs
arrive and accepted shares start flowing.

### 0% GPU util but the solver process appears to be running

Silent CPU fallback. The solver binary doesn't have a CUDA kernel
image compatible with your driver. The installer's smoke test catches
this — if you skipped it, run:

```bash
nvidia-smi --query-compute-apps=pid,process_name --format=csv,noheader
```

If `btx-gbt-solve` isn't listed while the miner is running, the
binary fell back to CPU silently. v0.2.3 ships native cubins for
sm_61 / sm_75 / sm_86 / sm_89 / sm_90 / sm_120, so this should be
rare. File an issue with your `nvidia-smi -q` output and we'll ship
a build that covers your hardware. **Don't downgrade your driver.**

---

## Tuning beyond defaults

```bash
# Sweep batch sizes (default: 32, 64, 128, 256 for 30 seconds each):
dexbtx-miner benchmark

# Custom sweep over multiple dimensions:
dexbtx-miner benchmark --batches 64,128,256 --prefetches 4,8,16 --workers 8,12,16

# Write the winning config straight to your config.yaml:
dexbtx-miner benchmark --write-config
```

See [docs/TUNING.md](docs/TUNING.md) for the canonical env-var
reference and why `tries_used` / `nonce64` are misleading per-card
performance counters (the only honest absolute measure is btxprice.com's
network-derived metric).

---

## Solo mining?

`dexbtx-miner` is pool-only. If you want to mine solo against your own
`btxd`, use the upstream `btx-gbt-miner.py` from the BTX source tree
(linked in [release-tooling/SHA256SUMS](release-tooling/SHA256SUMS)) —
it speaks GBT directly to a local node and skips stratum entirely.

We don't ship a solo path here because (a) the pool's block-forwarder,
peer-mesh tuning, and PPLNS smoothing are the entire value proposition,
and (b) running your own btxd well (peers, dbcache, reorg recovery)
is more work than most miners want to take on.

---

## Contributing / building from source

```bash
git clone https://github.com/dexbtx/minebtx
cd dexbtx-miner

# Run the Python wrapper directly:
pip install --user -e .
dexbtx-miner --config config.example.yaml

# Run tests (requires btx-gbt-solve in $PATH or BTX_GBT_SOLVE env):
python3 tests/test_gbt_solve_wrapper.py
```

The solver binary itself is built from
[Bitcoin Knots fork BTX source](https://github.com/btx-chain) with
the `--share-target` and daemon-mode patches applied (see
[release-tooling/README.md](release-tooling/README.md) for the build
recipe + portability requirements).

---

## License

[MIT](LICENSE) — fork it, ship it, modify it, sell it. We just ask that
you don't strip the copyright notice from your distribution.

---

## Where to ask questions

- **Pool TG**: [@btxdexbot](https://t.me/btxdexbot) (`/help` for commands)
- **GitHub Issues**: https://github.com/dexbtx/minebtx/issues
- **Network info**: [btxprice.com](https://btxprice.com)
