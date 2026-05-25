# Onboarding

## Why pool here first

Before the install steps, a one-screen summary of what you're buying into
by pointing your hashrate at DEXBTX:

- **Variance smoothing.** A 5070-class card solo-mines a BTX block every
  4-7 days *on average* but variance is brutal (3-week droughts happen).
  PPLNS pays you per-share against a rolling 10K-share window, so your
  income tracks your hashrate instead of your luck.
- **Block propagation that wins races.** BTX's P2P mesh is small and
  slow. We run a dedicated **block-forwarder** with ancestor-push that
  injects every win straight into the highest-connectivity peers the
  millisecond the solver returns — taking our fleet orphan rate from
  ~38% (raw internet routing) down to under 5%. You inherit that
  propagation just by pointing your miner at us.
- **Pre-tuned peer mesh.** Our pool node maintains a curated regional
  peer graph across NA / EU / SEA / AU. You don't spend the first week
  hunting non-stalling peers; templates route through ours by default.
- **Failure modes already paved over.** A year of running a 7-rental
  fleet uncovered shielded-state corruption after reorg storms, btxd
  hangs on slow peer block-body delivery, tailscaled crashes in
  userspace containers, CUDA buffer-pool exhaustion at batch=256, and
  silent CPU fallback (mining at 1/1000th throughput for weeks
  unnoticed). The installer's hard CUDA-engagement smoke test plus the
  miner's reconnect backoff + daemon respawn cover all of those.
- **Simplicity.** Solo mining = run your own btxd (600 GB chainstate,
  manual peer wrangling, manual restart after reorg storms). Pooled
  mining = `curl | bash | mine`. One binary, one YAML, one tmux.
- **Transparent pool economics.** 2.5% fee, weekly Friday 18:00 UTC
  batched payouts, no minimum-payout threshold beyond the network dust
  floor. Pool treasury + fee receiving addresses published on the
  dashboard.

---

## Pick your starting point

Three paths into the pool, in order of audience size:

| You are… | Path |
|---|---|
| **Fresh starter** — empty Linux box, never mined BTX before | [Start here](#fresh-starter-empty-box) |
| **Converter** — already solo-mining BTX, want to pool instead | [Start here](#converter-already-solo-mining) |
| **Self-hoster** — running your own btxd, want pool-mining alongside it | [Start here](#self-hoster-pool-mining-alongside-your-own-btxd) |

All three paths end at the **same one-liner**. Read on for why even converters should re-run it.

---

## Fresh starter (empty box)

You have a Linux machine with an NVIDIA GPU (Pascal GTX 1070 through Blackwell RTX 5090) and nothing BTX-related installed yet.

### Prerequisites

- Linux (Ubuntu 22.04+ tested; other distros likely work) or macOS, or Windows via WSL2
- NVIDIA driver ≥ 565 (`nvidia-smi --query-gpu=driver_version --format=csv,noheader`)
- ~500 MB free disk
- Stable internet route to `stratum.minebtx.com:3333` (no Tailscale or VPN required)
- A BTX payout address — see [Getting a BTX address](#getting-a-btx-address) below if you don't have one

### Install (one command)

```bash
curl -fsSL https://minebtx.com/install.sh | bash -s -- --address 'btx1z…YOUR_BTX_ADDRESS…'
```

That's it. The script:

1. **Detects your GPU** (`nvidia-smi --query-gpu=name`) and picks the matched profile from the per-card table — see [TUNING.md](TUNING.md)
2. **Installs Python 3.10+ and `pyyaml`** if missing (`apt-get install` on Debian/Ubuntu; manual instructions printed otherwise)
3. **Downloads `btx-gbt-solve`** as a GitHub release asset, SHA256-verifies against the value pinned in `pyproject.toml`, refuses to proceed on mismatch
4. **Runs a hard GPU-acceleration smoke test** (Phase 0.3b): asserts the solver appears in `nvidia-smi --query-compute-apps`, draws >100 W sustained, and exceeds 1000 N/s throughput. Fails LOUDLY if CUDA didn't actually engage — catches silent CPU fallback that would otherwise mine at 1/1000th speed without you noticing
5. **Writes** `~/.dexbtx-miner/config.yaml` with your address, the GPU's tuned profile, and stratum endpoint
6. **Installs the `dexbtx-miner` Python orchestrator** (this repo) via `pip install --user dexbtx-miner`

### Run

```bash
dexbtx-miner --config ~/.dexbtx-miner/config.yaml
```

Or as a persistent tmux session:

```bash
tmux new -d -s dexbtx 'dexbtx-miner --config ~/.dexbtx-miner/config.yaml 2>&1 | tee -a ~/.dexbtx-miner/miner.log'
tmux attach -t dexbtx
```

You should see `share OK` lines in the log within a minute. Now [pick a worker name + link Telegram](#worker-naming-and-telegram-notifications).

---

## Converter (already solo-mining)

You're already mining BTX solo with your own btxd and `btx-gbt-solve`. You want to point your hashrate at the pool instead.

**You should run the same install.sh as fresh starters.** Here's why — solo-mining-era setups have several incompatibilities with the pool-mining path that the installer fixes for you:

| Component | Solo-mining era (your current setup) | Pool-mining required |
|---|---|---|
| `btx-gbt-solve` binary | Often pre-v4.0 — missing `--share-target` + `--daemon` flags | v4.3+ with `--share-target` (pool early-exit) and `--daemon` (persistent solver, eliminates per-slice CUDA-context-init cost) |
| Solver kernel for your GPU | Pascal-only sm_61 native; PTX-JIT to everything else | Native cubins for sm_61 + sm_89 + sm_90 + sm_120 (Pascal + Ada + Hopper + Blackwell). Turing/Ampere still PTX-JIT |
| `BTX_MATMUL_GPU_INPUTS` | Often unset → defaults to `1` (GPU-generated inputs) | `0` (CPU-generated). Mandatory — without this every modern GPU caps at ~8% util / 33 W |
| Tuning profile | `BTX_MATMUL_PREPARE_WORKERS=8`, `SOLVER_THREADS=4`, `BATCH=32` (old Pascal-era defaults) | `PREPARE_WORKERS=16`, `SOLVER_THREADS=8`, `BATCH=128` — universal across Pascal through Blackwell. **Sustained `workers=16` is the lever that takes a 5070 from 70% util to 100%** |
| Env-var names you might have set | `BTX_MATMUL_PREFETCH`, `BTX_MATMUL_BATCH_SIZE` (don't exist) | `BTX_MATMUL_PREPARE_PREFETCH_DEPTH`, `BTX_MATMUL_SOLVE_BATCH_SIZE` (the actual names). Wrong names are silent no-ops |
| Orchestrator | `btx-gbt-miner.py` (solo-mining, polls your local btxd) | `dexbtx-miner` (stratum client, drives the solver in daemon mode) |
| Stratum protocol | n/a | Pool stratum v1 with matmul-extended job format |

**The install.sh handles ALL of this for you.** It downloads the right binary, writes the right config, verifies CUDA actually engages, and points everything at the pool.

If you really want to do it by hand instead, see [Manual converter recipe](#manual-converter-recipe) below — but unless you have a non-standard setup you really need to preserve, just run the one-liner.

### Will install.sh disturb my existing setup?

Mostly no:

- ✅ Installs new files under `~/.dexbtx-miner/` — doesn't touch your existing `~/.btx`, `/root/.btx`, or any other btxd datadir
- ✅ Installs `dexbtx-miner` Python package under `pip --user` — doesn't conflict with system Python
- ✅ Idempotent — re-running upgrades to latest, preserves your existing config.yaml (with `--yes` it backs up the old)
- ⚠️ Replaces the solver binary at `~/.dexbtx-miner/bin/btx-gbt-solve`. If you had a custom solver at that exact path, it gets overwritten. If your solo solver is elsewhere (e.g. `/usr/local/bin`, your build dir), no conflict

### After install.sh: stopping your solo mining

If you want to fully convert (stop solo mining, only pool-mine):

```bash
# Stop your solo orchestrator
pkill -f btx-gbt-miner.py    # if you use the legacy Python orchestrator
# Or whatever you use to drive your solo solver

# Optional: keep btxd running anyway — it's useful as a local validation node + peer
# even if you're not solo-mining (and you can switch back to solo by stopping
# dexbtx-miner and re-starting your orchestrator)
```

If you want to mine **both** solo AND pool — see [Self-hoster](#self-hoster-pool-mining-alongside-your-own-btxd) below; you can run both, but only one can have the GPU at a time.

### Manual converter recipe

For users who want full control (or have an exotic setup):

1. Build (or download) the v4.3+ `btx-gbt-solve` binary with both `--share-target` AND `--daemon` flags. Verify:
   ```bash
   btx-gbt-solve --help | grep -E "share-target|daemon"
   # Both must be present
   ```
2. Verify the binary has the right native cubins (Pascal sm_61 mandatory; sm_89/90/120 if you have those cards):
   ```bash
   cuobjdump --list-elf btx-gbt-solve
   ```
3. Install dexbtx-miner: `pip install --user dexbtx-miner` (or `pip install --user -e .` from this repo)
4. Write `~/.dexbtx-miner/config.yaml` — copy [config.example.yaml](../config.example.yaml) and edit `payout_address` + `worker_name`. Pick the right tuning profile from [TUNING.md](TUNING.md) for your card
5. Run: `dexbtx-miner --config ~/.dexbtx-miner/config.yaml`

---

## Self-hoster (pool-mining alongside your own btxd)

You run your own btxd and want to pool-mine while keeping the node up for sovereignty / peer-graph / dashboard / whatever.

**Run the same install.sh.** It doesn't touch your btxd or its datadir. Both can run simultaneously:

- `btxd` keeps doing its thing (full node, peering, RPC for your local tools)
- `dexbtx-miner` drives `btx-gbt-solve` against the pool stratum

The only conflict point is the GPU itself — if you were ALSO solo-mining (driving the solver yourself with `btx-gbt-miner.py` against your local btxd's `getmatmulchallenge`), you need to stop that, because two processes can't share the same GPU efficiently. Pool-mining wins on share variance for almost all card classes (especially below 5-10% network share).

### Reading peer recommendations from the pool

If your btxd is struggling for peers (the public DNS seeds are spotty), pull the pool's curated list:

```bash
curl -s https://stratum.minebtx.com/api/peer_list \
  | jq -r '.peers[].addr' \
  | xargs -I {} btx-cli addnode {} add
```

The list is filtered to peers on the current consensus subver, on the pool's tip, and not blacklisted. Use `?relaxed=true` if you want all observed peers (e.g. for diversity / regional clustering) instead of just the recommended subset.

---

## Getting a BTX address

If you don't have a BTX address yet, you need a wallet. Options:

- **Run your own btxd:** clone the BTX repo, build, `btx-cli getnewaddress` returns a transparent `btx1z…` address
- **Ask in the Telegram group:** [@btxdexbot](https://t.me/btxdexbot) — community members will help bootstrap

A valid pool payout address looks like `btx1z` followed by ~58 alphanumeric characters. It's a witness-v2 (P2MR) address that uses the post-quantum signature scheme — that's why it's longer than a Bitcoin address.

---

## Worker naming and Telegram notifications

The `worker_name` field in your config controls how your rig appears on the dashboard + how it's identified for Telegram DMs.

### Naming convention

- **Allowed characters:** alphanumeric, `-`, `_`. Keep it short — stratum has a string length cap.
- **Recommended pattern:** `<alias>-<gpu>-<country2>-<color>` (four dash-separated parts, all lowercase):
  - `<alias>` — your nickname for this rig (any handle; **don't use your real name**)
  - `<gpu>` — short GPU model: `1070`, `2080ti`, `3060ti`, `4060ti`, `5070`, `5070ti`, …
  - `<country2>` — ISO 3166-1 alpha-2 country code: `us`, `de`, `ca`, `gb`, `jp`, …
  - `<color>` — a short, unique-to-you suffix to disambiguate multiple identical rigs
  - **Examples:** `kappa-5060ti-us-blue`, `bravo-1070-de-red`, `tango-4060ti-ca-green`
- **Why the country + color tail?** Two miners both running 5060 Tis in the US would otherwise collide on `home-5060ti`. The country + color keeps every worker row globally unique on the dashboard and `/link`-routable from the bot without ambiguity.
- **Uniqueness scope:** the pool keys workers on `(payout_address, worker_name)`, so your `kappa-5060ti-us-blue` doesn't collide with someone else's *payout address*. The convention exists for the **dashboard** view, which lists workers across all addresses.
- **Stable identity:** changing the name creates a new worker row in the pool DB. Earned shares stay with the old name (PPLNS doesn't migrate). Pick one and stick with it.

### Telegram DM hookup

The pool's bot lets you link your TG chat to a worker so the weekly payout transaction DMs you directly. All commands:

| Command | What it does |
|---|---|
| `/stats` | Pool aggregate: hashrate, workers active, blocks found today, etc. |
| `/mybalance <worker_name>` | Accrued balance (sat + BTX) + last payout date for that worker |
| `/blocks_today` | List of blocks found since 00:00 UTC, with maturity status |
| `/link <worker_name>` | Bind your TG chat to a worker — payout broadcast DMs you the receipt |
| `/unlink <worker_name>` | Undo `/link` for that worker |
| `/myblock <worker_name>` | Your contribution (credit_sat) to each recent block found |
| `/help` | Print the full command list |

### Setup flow (3 steps)

1. **Pick a worker_name** and add it to your `~/.dexbtx-miner/config.yaml`
2. **Start mining** — `dexbtx-miner --config ~/.dexbtx-miner/config.yaml`. The pool creates a worker row the first time you submit a share
3. **DM the bot** to claim the worker:
   ```
   /link kappa-5060ti-us-blue
   ```
   You'll get back `🔗 Linked. Worker <name> will now DM you here on payout broadcasts.`

Now every Friday 18:00 UTC payout, you get a DM with the payout transaction details (amount, txid, block height). No need to refresh the dashboard.

### What if I have multiple workers?

`/link` is per-worker. Link each one separately:

```
/link kappa-1070-us-blue
/link kappa-5070-us-red
/link kappa-4060ti-us-green
```

Each gets DM'd independently on payouts. The bot doesn't merge balances across your workers — `/mybalance` shows one worker at a time. But the dashboard's "by address" rollup view (under construction) will eventually sum across all your workers for a single address.

---

## Verify it's working

After the miner has been running for >1 minute:

```bash
# 1. Daemon alive (single long-running process)
pgrep -af "btx-gbt-solve.*--daemon"
# → one PID

# 2. CUDA actually engaged
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader
# → should list btx-gbt-solve

# 3. GPU util at target
nvidia-smi --query-gpu=utilization.gpu,power.draw --format=csv,noheader
# → ≥95% util for non-Pascal; ≥80% for Pascal (memory-bound — TUNING.md)

# 4. Shares being accepted
grep -cE 'share OK' ~/.dexbtx-miner/miner.log
# → counter ticking up

# 5. No persistent rejections or daemon I/O errors
grep -E 'share REJECTED|daemon I/O failed|unexpected daemon' ~/.dexbtx-miner/miner.log | tail -5
# → A few `share REJECTED` lines right after startup (codes 21/22/23) are
#   normal post-reconnect noise. They should stop within 1-2 minutes. If
#   they persist past 3 minutes, see the rejection-codes table at the
#   bottom of this doc.
```

Then on Telegram: `/mybalance <your_worker_name>`. If the bot shows a non-zero balance, you're contributing to the pool and earning shares.

---

## Operational notes

### Pool fee
2.5% of each block reward goes to the pool treasury (transparently published on the dashboard). The remaining 97.5% distributes via PPLNS over the most recent 10,000 shares.

### Payouts
Every Friday 18:00 UTC. Single batched multi-output transaction (low on-chain fee per recipient). No minimum threshold — only the network dust floor (10K sats ≈ 0.0001 BTX). Below dust, your balance rolls over to the next week.

### Variance
At small pool share, expect drought windows of 1–3 days between block finds. Pool share grows with the miner count — see `/stats` in the TG bot for current totals.

### Want to switch back to solo?
`dexbtx-miner` is pool-only. For solo mining, use the upstream
`btx-gbt-miner.py` from the BTX source tree — it speaks GBT directly
to a local `btxd` and skips stratum entirely.

---

## Support

- **Telegram bot**: [@btxdexbot](https://t.me/btxdexbot) — `/help` for the command list
- **Issues**: https://github.com/dexbtx/minebtx/issues
- **Pool dashboard**: https://pool.minebtx.com
- **Network data**: [btxprice.com](https://btxprice.com)

---

## Common rejection codes (post-install hiccups)

| Pool error code | Meaning | What to do |
|---|---|---|
| 21 — `job stale (>180s)` | Your slice took too long; submitted past the 180s freshness window | Normal post-reconnect noise. Drops to zero once stratum settles |
| 22 — `duplicate share` | Same nonce was already submitted (often during reconnect-replay) | Self-corrects within 1-2 min |
| 23 — `digest >= share_target` | Pool difficulty changed mid-job | Self-corrects on next `notify` |
| 27 — `bad seeds` | Your solver's matmul consensus params don't match the pool's (rare — would indicate a network upgrade you missed) | Re-run `install.sh` to get the latest binary |

If `share OK` lines never appear and you're getting these continuously past the first 2 minutes, file an issue with the last 50 log lines.
