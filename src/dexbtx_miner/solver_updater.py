"""Auto-update the local `btx-gbt-solve` binary against a published manifest.

The dexbtx project publishes a JSON manifest at a stable URL describing the
current canonical solver binary (version, SHA256, download URL). On every
miner startup we fetch that manifest, compare against our local binary's
SHA, and download + replace if they differ.

This eliminates the "stale binary on a long-running rig" class of problems
— ESPECIALLY useful at fork boundaries, where we can publish the new binary
and every miner picks it up on its next process spawn without us having to
chase down operators via Telegram.

Trust model is identical to the original install.sh flow: SHA-pinned in a
manifest at a URL the operator already trusts. Opt-out via the
`DEXBTX_NO_SOLVER_AUTOUPDATE=1` env var.

Failure behavior is "fail open": any error (network, parse, SHA mismatch
on download) logs a warning and lets mining continue with the current
local binary. The one exception is `min_required_sha256`: if the manifest
declares a hard floor and the local binary doesn't match it AND we can't
upgrade to it, we refuse to start mining (raise an exception). This is
the lever for fork-mandatory upgrades.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Canonical manifest URL. Lives in the public dexbtx/minebtx repo root so
# the source of truth is git-tracked alongside install.sh.
DEFAULT_MANIFEST_URL = (
    "https://github.com/dexbtx/minebtx/raw/main/.solver-channel.json"
)

# Network timeouts. These are generous because the check runs once at
# startup and we'd rather wait than fail; still bounded so a wedged DNS
# doesn't keep the miner from coming up.
_MANIFEST_TIMEOUT_S = 15
_DOWNLOAD_TIMEOUT_S = 300


class SolverUpdateRequired(RuntimeError):
    """Raised when `min_required_sha256` is set, local doesn't match, and
    auto-update couldn't satisfy the requirement. Mining must not proceed."""


def _sha256_file(path: str | os.PathLike[str]) -> str | None:
    """Return hex SHA256 of `path`, or None if it doesn't exist."""
    p = Path(path)
    if not p.exists():
        return None
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch_manifest(url: str) -> dict[str, Any] | None:
    """Fetch + parse the manifest. Returns None on any error (fail open)."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "dexbtx-miner-updater"},
        )
        with urllib.request.urlopen(req, timeout=_MANIFEST_TIMEOUT_S) as r:
            body = r.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("solver auto-update: manifest fetch failed: %s", e)
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError as e:
        log.warning("solver auto-update: manifest parse failed: %s", e)
        return None


def _download_and_verify(
    url: str,
    expected_sha256: str,
    dest_dir: Path,
) -> Path | None:
    """Download `url` to a temp file in `dest_dir`, verify SHA, return path.

    Same dir as the destination so the eventual `os.replace` is atomic
    (must be same filesystem). Caller is responsible for renaming + chmod.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_s = tempfile.mkstemp(
        prefix=".btx-gbt-solve.dl.",
        dir=str(dest_dir),
    )
    tmp_path = Path(tmp_path_s)
    os.close(fd)
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "dexbtx-miner-updater"},
        )
        with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT_S) as r, \
             tmp_path.open("wb") as out:
            shutil.copyfileobj(r, out, length=1 << 20)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("solver auto-update: download failed: %s", e)
        tmp_path.unlink(missing_ok=True)
        return None

    actual = _sha256_file(tmp_path)
    if actual != expected_sha256:
        log.warning(
            "solver auto-update: download SHA mismatch (expected %s, got %s)",
            expected_sha256, actual,
        )
        tmp_path.unlink(missing_ok=True)
        return None
    return tmp_path


def _install_atomic(src: Path, dest: Path) -> None:
    """Move `src` to `dest` (same fs), backing up the prior file if any."""
    if dest.exists():
        backup = dest.with_suffix(dest.suffix + ".pre-autoupdate-bak")
        shutil.copy2(dest, backup)
    src.chmod(0o755)
    os.replace(src, dest)


def maybe_update_solver(
    solver_path: str | os.PathLike[str],
    *,
    manifest_url: str = DEFAULT_MANIFEST_URL,
) -> bool:
    """Check the manifest and replace the local solver binary if outdated.

    Returns True if the binary was updated, False otherwise. Never raises
    on routine errors — fails open so a network blip can't keep miners
    offline. Only raises `SolverUpdateRequired` when a hard-floor manifest
    constraint can't be satisfied and mining therefore must not proceed.
    """
    if os.environ.get("DEXBTX_NO_SOLVER_AUTOUPDATE", "0") == "1":
        log.info("solver auto-update: disabled by DEXBTX_NO_SOLVER_AUTOUPDATE=1")
        return False

    solver_path = Path(solver_path).expanduser()
    started = time.time()
    manifest = _fetch_manifest(manifest_url)
    if manifest is None:
        return False  # already warned

    target_sha = manifest.get("sha256")
    target_url = manifest.get("url")
    target_ver = manifest.get("version", "unknown")
    min_required = manifest.get("min_required_sha256")
    reason = manifest.get("force_upgrade_reason")

    if not isinstance(target_sha, str) or len(target_sha) != 64:
        log.warning("solver auto-update: manifest missing valid sha256")
        return False
    if not isinstance(target_url, str) or not target_url.startswith("http"):
        log.warning("solver auto-update: manifest missing valid url")
        return False

    current_sha = _sha256_file(solver_path)
    if current_sha == target_sha:
        log.info(
            "solver auto-update: up-to-date (version=%s sha=%s) [check=%.0fms]",
            target_ver, target_sha[:12], (time.time() - started) * 1000,
        )
        return False

    log.info(
        "solver auto-update: %s -> %s (reason=%s)",
        (current_sha or "missing")[:12],
        target_sha[:12],
        reason or "routine",
    )

    new_tmp = _download_and_verify(
        target_url, target_sha, solver_path.parent,
    )
    if new_tmp is None:
        # download failed; check whether we're under a hard floor
        if isinstance(min_required, str) and current_sha != min_required:
            raise SolverUpdateRequired(
                f"manifest requires sha256={min_required[:12]}... but local is "
                f"{(current_sha or 'missing')[:12]}... and download failed; "
                f"refusing to mine. Set DEXBTX_NO_SOLVER_AUTOUPDATE=1 to override."
            )
        return False

    try:
        _install_atomic(new_tmp, solver_path)
    except OSError as e:
        log.warning("solver auto-update: install failed: %s", e)
        new_tmp.unlink(missing_ok=True)
        return False

    log.info(
        "solver auto-update: installed version=%s sha=%s [total=%.0fms]",
        target_ver, target_sha[:12], (time.time() - started) * 1000,
    )
    return True
