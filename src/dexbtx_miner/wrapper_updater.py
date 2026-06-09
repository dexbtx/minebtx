"""Wrapper-side self-upgrade.

v0.4.1: at process start the wrapper checks the channel manifest's
`version` field. If it's newer than our installed `__version__`, we
pip-install the new tag's tarball (mirroring install.sh's path) and
re-exec the same argv. From v0.4.1 onward the fleet keeps the Python
wrapper current with no operator intervention.

Mirrors `solver_updater.py`'s defaults — same manifest URL, same
fail-open semantics. Any failure (network, parse, pip, exec) logs a
warning and lets the miner continue with the wrapper that's already
installed; never throws.

Opt-out: set `DEXBTX_NO_WRAPPER_AUTOUPDATE=1` in the env.
Override pkg URL: set `DEXBTX_MINER_PKG_URL_TEMPLATE` (must contain
`{version}`). Override manifest URL: set `DEXBTX_MANIFEST_URL`.

Loop-protection: before exec we set `DEXBTX_WRAPPER_JUST_UPGRADED=<v>`
in the child env. On the next start that env var is checked against the
NEWLY-loaded `__version__`; if they match, we skip the upgrade attempt.
So a broken release can't infinite-loop the operator's process.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request

from . import __version__

log = logging.getLogger(__name__)

DEFAULT_MANIFEST_URL = (
    "https://github.com/dexbtx/minebtx/raw/main/.solver-channel.json"
)
DEFAULT_PKG_URL_TEMPLATE = (
    "https://github.com/dexbtx/minebtx/archive/refs/tags/v{version}.tar.gz"
)
_MANIFEST_TIMEOUT_S = 8
_PIP_TIMEOUT_S = 300


def _parse_version(v: str | None) -> tuple[int, ...]:
    """Tuple-compare semver-ish strings. Unparseable → (0,) sorts lowest."""
    if not v:
        return (0,)
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, AttributeError):
        return (0,)


def _fetch_manifest_version(url: str) -> str | None:
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "dexbtx-miner-wrapper-updater"},
        )
        with urllib.request.urlopen(req, timeout=_MANIFEST_TIMEOUT_S) as r:
            data = json.loads(r.read())
        v = data.get("version")
        if not isinstance(v, str) or not v:
            return None
        return v
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("wrapper auto-update: manifest fetch failed: %s", e)
    except json.JSONDecodeError as e:
        log.warning("wrapper auto-update: manifest parse failed: %s", e)
    return None


def _run_pip_install(pkg_url: str) -> bool:
    """Try `pip install --user --upgrade`, fall back to --break-system-packages.

    Returns True if either attempt succeeded.
    """
    base_cmd = [
        sys.executable, "-m", "pip", "install",
        "--user", "--upgrade", "--quiet", pkg_url,
    ]
    try:
        r1 = subprocess.run(
            base_cmd, capture_output=True, text=True, timeout=_PIP_TIMEOUT_S,
        )
        if r1.returncode == 0:
            return True
    except subprocess.TimeoutExpired:
        log.warning("wrapper auto-update: pip-install timed out (%ds)", _PIP_TIMEOUT_S)
        return False
    except Exception as e:  # noqa: BLE001 — fail-open per design
        log.warning("wrapper auto-update: pip-install raised: %s", e)
        return False

    # PEP-668 retry — mirror install.sh's pip_install() fallback.
    try:
        r2 = subprocess.run(
            base_cmd + ["--break-system-packages"],
            capture_output=True, text=True, timeout=_PIP_TIMEOUT_S,
        )
        if r2.returncode == 0:
            return True
    except Exception as e:  # noqa: BLE001
        log.warning("wrapper auto-update: PEP-668 retry raised: %s", e)
        return False

    log.warning(
        "wrapper auto-update: pip-install failed both with and without "
        "--break-system-packages. stderr=%r",
        ((r1.stderr or "") + (r2.stderr or ""))[:300],
    )
    return False


def maybe_self_upgrade() -> None:
    """Check manifest, pip-upgrade + re-exec if a newer wrapper is published.

    Called once at the very top of `main()` (before solver auto-update,
    so a wrapper upgrade ships any new solver-update logic too). Returns
    on no-op or failure. On success, `os.execvpe` replaces the current
    process and doesn't return.
    """
    if os.environ.get("DEXBTX_NO_WRAPPER_AUTOUPDATE"):
        log.debug("wrapper auto-update: opted out via env")
        return

    # Loop-guard. The child process started by a successful upgrade
    # inherits this env var. If we get back here with the env var equal
    # to our newly-loaded __version__, the upgrade just happened and
    # we're up to date — don't even try again this run.
    just = os.environ.pop("DEXBTX_WRAPPER_JUST_UPGRADED", None)
    if just and _parse_version(just) <= _parse_version(__version__):
        log.info("wrapper auto-update: post-upgrade restart confirmed at v%s", __version__)
        return

    manifest_url = os.environ.get("DEXBTX_MANIFEST_URL", DEFAULT_MANIFEST_URL)
    target = _fetch_manifest_version(manifest_url)
    if target is None:
        return  # fail-open
    if _parse_version(target) <= _parse_version(__version__):
        log.debug(
            "wrapper auto-update: current=%s target=%s — up to date",
            __version__, target,
        )
        return

    log.info("wrapper auto-update: upgrading wrapper %s → %s", __version__, target)
    pkg_url = os.environ.get(
        "DEXBTX_MINER_PKG_URL_TEMPLATE", DEFAULT_PKG_URL_TEMPLATE,
    ).format(version=target)
    if not _run_pip_install(pkg_url):
        return  # logged; fail-open

    new_env = os.environ.copy()
    new_env["DEXBTX_WRAPPER_JUST_UPGRADED"] = target
    log.info("wrapper auto-update: pip ok; re-exec as v%s", target)
    try:
        os.execvpe(sys.executable, [sys.executable, *sys.argv], new_env)
    except OSError as e:
        log.error("wrapper auto-update: execv failed: %s; continuing on current", e)
        return
