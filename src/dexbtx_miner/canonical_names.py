"""Local cache of pool-assigned canonical names.

The pool sends `mining.set_canonical_name` once per GPU after the first
hardware report. We cache the assignment keyed by `gpu_uuid` in
`~/.dexbtx-miner/canonical_names.json` so reconnects retain the info
even if the operator misses the startup log line.

Format:
    {
      "GPU-12345abc-...": {
        "canonical_name": "5090-ALPHA-1",
        "operator_label": "rtx5090-andy",
        "assigned_at": 1779912345
      },
      ...
    }
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_CACHE_PATH = Path.home() / ".dexbtx-miner" / "canonical_names.json"


def load(path: Path = DEFAULT_CACHE_PATH) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError) as e:
        log.warning("canonical_names cache unreadable at %s: %s — starting fresh", path, e)
    return {}


def save(cache: dict[str, dict[str, Any]], path: Path = DEFAULT_CACHE_PATH) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache, indent=2, sort_keys=True))
        os.replace(tmp, path)
    except OSError as e:
        log.warning("could not persist canonical_names cache to %s: %s", path, e)


def upsert(
    gpu_uuid: str,
    canonical_name: str,
    operator_label: str | None,
    assigned_at: int,
    path: Path = DEFAULT_CACHE_PATH,
) -> dict[str, dict[str, Any]]:
    cache = load(path)
    cache[gpu_uuid] = {
        "canonical_name": canonical_name,
        "operator_label": operator_label,
        "assigned_at": assigned_at,
    }
    save(cache, path)
    return cache


def format_assignment_banner(
    assignments: list[dict[str, Any]],
    dashboard_url: str = "https://pool.minebtx.com/dashboard",
) -> str:
    """Format a prominent multi-line banner for stdout.

    `assignments` is a list of {canonical_name, operator_label, gpu_uuid, ...}
    dicts (newly-received from the pool, not the local cache shape).
    """
    if not assignments:
        return ""
    bar = "=" * 60
    lines = [bar]
    if len(assignments) == 1:
        a = assignments[0]
        lines.append("[pool] Canonical name assigned for this rig:")
        lines.append(f"       {a['canonical_name']}"
                     + (f"  (operator label: {a['operator_label']})"
                        if a.get('operator_label') else ""))
        lines.append("       Find it on the dashboard at:")
        lines.append(f"       {dashboard_url}#{a['canonical_name']}")
    else:
        op_label = assignments[0].get("operator_label")
        lines.append("[pool] Canonical names assigned:")
        for i, a in enumerate(assignments):
            lines.append(f"       GPU {i} → {a['canonical_name']}")
        if op_label:
            lines.append(f"       (operator label: {op_label})")
        lines.append(f"       Dashboard: {dashboard_url}")
    lines.append(bar)
    return "\n".join(lines)
