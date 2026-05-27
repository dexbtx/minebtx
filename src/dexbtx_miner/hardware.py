"""Hardware fingerprint + runtime metrics collection.

Builds the `hardware` dict sent in `mining.subscribe` (one-shot at connect)
and the periodic `worker.report_metrics` payload (every 60s). All
collection is best-effort — missing tooling produces `None` fields rather
than failing the connection.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import subprocess
from typing import Any

log = logging.getLogger(__name__)

# Cap subprocess wait so a hung nvidia-smi (rare but observed) doesn't
# stall the mining session.
SUBPROCESS_TIMEOUT_SEC = 5.0


def _run(cmd: list[str]) -> str | None:
    try:
        out = subprocess.check_output(
            cmd, stderr=subprocess.DEVNULL, timeout=SUBPROCESS_TIMEOUT_SEC
        )
        return out.decode("utf-8", errors="replace").strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _cpu_model() -> str | None:
    """First "model name" line from /proc/cpuinfo (Linux), or platform fallback."""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    if platform.system() == "Darwin":
        out = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
        if out:
            return out
    if platform.system() == "Windows":
        out = _run(["wmic", "cpu", "get", "name"])
        if out:
            lines = [l.strip() for l in out.splitlines() if l.strip() and "Name" not in l]
            if lines:
                return lines[0]
    return platform.processor() or None


def _cpu_threads_total() -> int | None:
    n = os.cpu_count()
    return int(n) if n else None


def _ram_gb_total() -> float | None:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return round(kb / (1024 * 1024), 2)
    except OSError:
        pass
    if platform.system() == "Darwin":
        out = _run(["sysctl", "-n", "hw.memsize"])
        if out and out.isdigit():
            return round(int(out) / (1024**3), 2)
    return None


def _ram_gb_used() -> float | None:
    try:
        with open("/proc/meminfo") as f:
            total_kb = None
            avail_kb = None
            for line in f:
                if line.startswith("MemTotal:"):
                    total_kb = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    avail_kb = int(line.split()[1])
            if total_kb is not None and avail_kb is not None:
                used_kb = total_kb - avail_kb
                return round(used_kb / (1024 * 1024), 2)
    except OSError:
        pass
    return None


def _os_string() -> str:
    sys = platform.system()
    rel = platform.release()
    if sys == "Linux":
        try:
            with open("/etc/os-release") as f:
                fields = {}
                for line in f:
                    if "=" in line:
                        k, v = line.split("=", 1)
                        fields[k.strip()] = v.strip().strip('"')
            name = fields.get("PRETTY_NAME") or fields.get("NAME", "Linux")
            return f"{name} / {rel}"
        except OSError:
            return f"Linux / {rel}"
    return f"{sys} / {rel}"


def _nvidia_query(fields: str) -> list[list[str]]:
    """Run `nvidia-smi --query-gpu=<fields> --format=csv,noheader,nounits` and
    return a list of per-GPU value lists. Empty list if nvidia-smi missing."""
    out = _run([
        "nvidia-smi",
        f"--query-gpu={fields}",
        "--format=csv,noheader,nounits",
    ])
    if not out:
        return []
    rows = []
    for line in out.splitlines():
        cells = [c.strip() for c in line.split(",")]
        rows.append(cells)
    return rows


def _driver_and_cuda() -> tuple[str | None, str | None]:
    """Returns (driver_version, cuda_version) tuple from `nvidia-smi`."""
    out = _run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader,nounits"])
    driver = None
    if out:
        # First GPU's driver_version (same across all GPUs on the host)
        driver = out.splitlines()[0].strip() or None
    cuda = None
    out = _run(["nvidia-smi"])
    if out:
        m = re.search(r"CUDA Version:\s*(\S+)", out)
        if m:
            cuda = m.group(1)
    return driver, cuda


def _enumerate_gpus() -> list[dict[str, Any]]:
    """Per-GPU static info: model, vram, compute capability, pcie link, uuid."""
    rows = _nvidia_query("name,memory.total,compute_cap,pcie.link.gen.current,pcie.link.width.current,uuid")
    gpus = []
    for r in rows:
        if len(r) < 6:
            continue
        model, vram_mb, cc, pcie_gen, pcie_width, uuid = r[:6]
        try:
            vram_gb = round(float(vram_mb) / 1024, 2) if vram_mb and vram_mb != "[Not Supported]" else None
        except ValueError:
            vram_gb = None
        compute_capability = f"sm_{cc.replace('.', '')}" if cc and cc != "[Not Supported]" else None
        pcie_link = None
        if pcie_gen and pcie_width and pcie_gen not in ("[Not Supported]", "[N/A]"):
            pcie_link = f"Gen{pcie_gen} x{pcie_width}"
        gpus.append({
            "model": model,
            "vram_gb": vram_gb,
            "compute_capability": compute_capability,
            "pcie_link": pcie_link,
            "gpu_uuid": uuid,
        })
    return gpus


def collect_static_hardware(miner_version: str, cpu_threads_allocated: int | None = None) -> dict[str, Any]:
    """One-shot fingerprint for `mining.subscribe`'s `hardware` dict.

    `cpu_threads_allocated` is how many threads the miner is *configured* to
    use (passed from --solver-threads). Distinct from `cpu_threads_total`
    which is the host's full thread count.
    """
    driver, cuda = _driver_and_cuda()
    gpus = _enumerate_gpus()
    return {
        "cpu_model": _cpu_model(),
        "cpu_threads_total": _cpu_threads_total(),
        "cpu_threads_allocated": cpu_threads_allocated,
        "ram_gb_total": _ram_gb_total(),
        "os": _os_string(),
        "miner_version": miner_version,
        "driver_version": driver,
        "cuda_version": cuda,
        "gpus": gpus,
    }


def _cpu_util_pct() -> float | None:
    """Whole-system CPU utilization over a 1-second window (Linux)."""
    try:
        a = _read_stat()
        if a is None:
            return None
        import time as _t
        _t.sleep(1.0)
        b = _read_stat()
        if b is None:
            return None
        idle_delta = b[3] - a[3]
        total_delta = sum(b) - sum(a)
        if total_delta <= 0:
            return None
        return round(100.0 * (1.0 - idle_delta / total_delta), 1)
    except Exception:
        return None


def _read_stat() -> tuple[int, ...] | None:
    try:
        with open("/proc/stat") as f:
            line = f.readline()
        if not line.startswith("cpu "):
            return None
        parts = line.split()[1:]
        return tuple(int(p) for p in parts[:7])  # user nice sys idle iowait irq softirq
    except OSError:
        return None


def _gpu_runtime() -> list[dict[str, Any]]:
    """Per-GPU runtime metrics: util%, power_w, temp_c, uuid."""
    rows = _nvidia_query("uuid,utilization.gpu,power.draw,temperature.gpu")
    out = []
    for r in rows:
        if len(r) < 4:
            continue
        uuid, util, power, temp = r[:4]
        try:
            util_pct = int(float(util))
        except ValueError:
            util_pct = None
        try:
            power_w = round(float(power), 1)
        except ValueError:
            power_w = None
        try:
            temp_c = int(float(temp))
        except ValueError:
            temp_c = None
        out.append({
            "gpu_uuid": uuid,
            "util_pct": util_pct,
            "power_w": power_w,
            "temp_c": temp_c,
        })
    return out


def collect_runtime_metrics(
    session_id: str,
    solver_nps: float | None,
    shares_session_total: int,
) -> dict[str, Any]:
    """Build a `worker.report_metrics` params payload (sent every 60s).

    `solver_nps` is the miner's last-known nonces-per-second figure
    (self-reported by the solver). Caller passes None if unknown.
    """
    return {
        "session_id": session_id,
        "timestamp": int(__import__("time").time()),
        "cpu_util_pct": _cpu_util_pct(),
        "ram_gb_used": _ram_gb_used(),
        "gpus": _gpu_runtime(),
        "solver_nps": solver_nps,
        "shares_session_total": shares_session_total,
    }


def hardware_summary_string(hw: dict[str, Any]) -> str:
    """One-line human summary for startup log. Hides None fields."""
    bits = []
    if hw.get("cpu_model"):
        bits.append(f"CPU={hw['cpu_model']}")
    if hw.get("cpu_threads_total"):
        bits.append(f"threads={hw['cpu_threads_total']}")
    if hw.get("ram_gb_total"):
        bits.append(f"RAM={hw['ram_gb_total']}GB")
    gpus = hw.get("gpus") or []
    if gpus:
        models = ", ".join(f"{g.get('model', '?')}" for g in gpus)
        bits.append(f"GPUs=[{models}]")
    if hw.get("driver_version"):
        bits.append(f"driver={hw['driver_version']}")
    if hw.get("cuda_version"):
        bits.append(f"cuda={hw['cuda_version']}")
    return " ".join(bits)
