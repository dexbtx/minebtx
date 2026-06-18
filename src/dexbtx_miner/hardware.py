"""Hardware fingerprint + runtime metrics collection.

Builds the `hardware` dict sent in `mining.subscribe` (one-shot at connect)
and the periodic `worker.report_metrics` payload (every 60s). All
collection is best-effort — missing tooling produces `None` fields rather
than failing the connection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import subprocess
import time
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


def _apple_chip_brand() -> str:
    """e.g. 'Apple M4', 'Apple M4 Pro', 'Apple M4 Max'. 'Apple Silicon' fallback."""
    out = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
    if out and out.startswith("Apple"):
        return out.strip()
    return "Apple Silicon"


def _apple_platform_uuid() -> str | None:
    """Stable, unique-per-machine identifier: IOPlatformUUID (preferred) or
    the hardware serial. Both come from IOPlatformExpertDevice via ioreg."""
    out = _run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"])
    if not out:
        return None
    m = re.search(r'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]+)"', out)
    if m:
        return m.group(1).lower()
    m = re.search(r'"IOPlatformSerialNumber"\s*=\s*"([^"]+)"', out)
    if m:
        return m.group(1).strip().lower()
    return None


def _apple_gpu_uuid() -> str:
    """Identity anchor the pool keys the canonical name on — must be UNIQUE
    per machine and stable across reconnects. Deliberately NOT a generic
    constant like 'apple-m4-0': that would collide with another miner's
    already-reserved name (incl. its client suffix) in the pool DB."""
    slug = re.sub(r"[^a-z0-9]", "", _apple_chip_brand().lower()) or "applesilicon"
    pid = _apple_platform_uuid() or (_hostname() or "unknown").lower()
    return f"apple-{slug}-{pid}"


def _apple_gpus() -> list[dict[str, Any]]:
    """Single integrated-GPU entry for Apple Silicon, shaped for the pool's
    canonical-name rule: `model` drives the name prefix (normalize → uppercase,
    strip ' GPU'/spaces: 'Apple M4 Pro GPU' → APPLEM4PRO) and `gpu_uuid` is the
    identity anchor. Without this the miner sends gpus:[] and the dashboard
    falls back to the raw hostname worker label."""
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return []
    return [{
        "model": f"{_apple_chip_brand()} GPU",
        "vram_gb": 0,                      # unified memory
        "compute_capability": "metal_3",
        "pcie_link": None,
        "gpu_uuid": _apple_gpu_uuid(),
    }]


def _rocm_smi_json(args: list[str]) -> dict[str, Any] | None:
    out = _run(["rocm-smi", *args, "--json"])
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _amd_gfx_target() -> str | None:
    """AMD GPU arch like 'gfx1100' (RDNA3). rocminfo is the most reliable source."""
    out = _run(["rocminfo"])
    if out:
        m = re.search(r"\bgfx\d{3,4}[a-z]?\b", out)
        if m:
            return m.group(0)
    j = _rocm_smi_json(["--showgcnversion"])
    if j:
        for info in j.values():
            for v in info.values():
                m = re.search(r"gfx\d{3,4}[a-z]?", str(v))
                if m:
                    return m.group(0)
    return None


def _amd_gpus() -> list[dict[str, Any]]:
    """Per-GPU info for AMD/ROCm hosts via rocm-smi (the HIP build is a separate
    binary from CUDA, so an AMD host has rocm-smi, not nvidia-smi). Shaped for
    the pool's canonical namer: model -> name prefix, gpu_uuid -> stable anchor.
    Returns [] when rocm-smi is absent (i.e. not an AMD/ROCm host)."""
    if platform.system() != "Linux":
        return []
    j = _rocm_smi_json(["--showproductname", "--showuniqueid", "--showmeminfo", "vram"])
    if not j:
        return []
    gfx = _amd_gfx_target()
    slug = re.sub(r"[^a-z0-9]", "", (gfx or "amdgpu").lower())
    gpus: list[dict[str, Any]] = []
    for card, info in j.items():
        if not str(card).lower().startswith("card"):
            continue
        low = {str(k).lower(): v for k, v in info.items()}
        model = (low.get("card series") or low.get("card model")
                 or low.get("device name") or low.get("gpu id") or "AMD GPU")
        model = str(model).strip() or "AMD GPU"
        if not re.search(r"amd|radeon|instinct", model, re.I):
            model = f"AMD {model}"
        uniq = str(low.get("unique id", "")).strip()
        ident = re.sub(r"[^a-z0-9]", "", uniq.lower()) if uniq else ""
        if not ident:
            ident = re.sub(r"[^a-z0-9]", "", str(card).lower()) + "-" + (_hostname() or "unknown").lower()
        vram_gb: float | None = None
        for k, v in low.items():
            if "vram total memory" in k:
                try:
                    vram_gb = round(int(str(v)) / (1024 ** 3), 2)
                except ValueError:
                    pass
        gpus.append({
            "model": model,
            "vram_gb": vram_gb if vram_gb is not None else 0,
            "compute_capability": gfx or "rocm",
            "pcie_link": None,
            "gpu_uuid": f"amd-{slug}-{ident}",
        })
    return gpus


def _enumerate_gpus() -> list[dict[str, Any]]:
    """Per-GPU static info: model, vram, compute capability, pcie link, uuid."""
    if platform.system() == "Darwin":
        return _apple_gpus()
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
    if not gpus:
        # No NVIDIA GPU — try AMD/ROCm (separate binary; rocm-smi present there).
        gpus = _amd_gpus()
    return gpus


def collect_static_hardware(
    miner_version: str,
    cpu_threads_allocated: int | None = None,
    solver_env: dict[str, str | int | None] | None = None,
    solver_path: str | None = None,
    payout_splits: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One-shot fingerprint for `mining.subscribe`'s `hardware` dict.

    `cpu_threads_allocated` is how many threads the miner is *configured* to
    use (passed from --solver-threads). Distinct from `cpu_threads_total`
    which is the host's full thread count.

    `solver_env` (v0.3.2+) carries the BTX_MATMUL_* env vars the miner is
    running with, so the pool can correlate config → performance and
    return data-backed tuning recommendations via
    `/api/worker_solver_recommendation`. The pool whitelists keys
    server-side, so passing extra keys is safe but useless.

    v0.3.4 adds four new categories so the pool's bucket-aware recommender
    can detect rental hosts, NUMA topology, and silent CPU fallback:
      - rental / containerization signals (cgroup quota, hostname, /.dockerenv)
      - NUMA topology + GPU node affinity (drives `numactl` recommendation)
      - active_backend probe (catches silent CPU fallback)
      - `power_cap_writable` (suppresses unactionable `nvidia-smi -pl` advice
        on rentals where the operator can't modify the cap)
    """
    driver, cuda = _driver_and_cuda()
    gpus = _enumerate_gpus()
    env = _detect_environment()
    numa = _collect_numa_topology(gpus)
    backend = _probe_active_backend(solver_path)
    out = {
        "cpu_model": _cpu_model(),
        "cpu_threads_total": _cpu_threads_total(),
        "cpu_threads_allocated": cpu_threads_allocated,
        "ram_gb_total": _ram_gb_total(),
        "os": _os_string(),
        "miner_version": miner_version,
        # v0.4.x — optional payout splits declared by this rig (fee legs;
        # remainder stays with payout_address). Pool honors only for this
        # session's own credits. Empty list when unused.
        "payout_splits": payout_splits or [],
        "driver_version": driver,
        "cuda_version": cuda,
        "gpus": gpus,
        # v0.3.4 fields — all NULL-tolerant on pool side
        "host_hostname": _hostname(),
        "is_containerized": env["is_containerized"],
        "cpu_threads_effective": env["cpu_threads_effective"],
        "rental_provider": env["rental_provider"],
        "power_cap_writable": env["power_cap_writable"],
        "numa": numa,                         # dict or None
        "active_backend": backend["active"],  # str or None
        "cuda_arch_supported": backend["cuda_archs"],  # str or None
    }
    if solver_env:
        out["solver_env"] = {k: (str(v) if v is not None else "") for k, v in solver_env.items()}
    return out


def _hostname() -> str | None:
    try:
        import socket
        return socket.gethostname()
    except Exception:
        return None


def _detect_environment() -> dict[str, Any]:
    """Detect whether we're on a containerized rental, and what the
    effective CPU allocation is. Five layered signals (any positive ⇒
    container). Effective threads come from cgroup CPU quota — the
    'real' budget vs `nproc`'s host total.
    """
    info = {
        "is_containerized": False,
        "cpu_threads_effective": None,
        "rental_provider": None,
        "power_cap_writable": False,
    }
    # /.dockerenv is the simplest signal
    try:
        import os.path as _op
        if _op.exists("/.dockerenv"):
            info["is_containerized"] = True
    except Exception:
        pass
    # cgroup file content
    try:
        with open("/proc/self/cgroup", "r") as f:
            cg = f.read()
        if any(m in cg for m in ("docker", "containerd", "kubepods", "lxc")):
            info["is_containerized"] = True
    except Exception:
        pass
    # cgroup v2 CPU quota
    try:
        with open("/sys/fs/cgroup/cpu.max", "r") as f:
            parts = f.read().strip().split()
        if len(parts) == 2 and parts[0] != "max":
            quota = int(parts[0])
            period = int(parts[1])
            if quota > 0 and period > 0:
                info["cpu_threads_effective"] = round(quota / period, 2)
    except Exception:
        # cgroup v1 fallback
        try:
            with open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", "r") as f:
                quota = int(f.read().strip())
            with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us", "r") as f:
                period = int(f.read().strip())
            if quota > 0 and period > 0:
                info["cpu_threads_effective"] = round(quota / period, 2)
        except Exception:
            pass
    # Rental provider hint from hostname
    hn = _hostname() or ""
    hn_low = hn.lower()
    if "vast.ai" in hn_low or "vast-" in hn_low:
        info["rental_provider"] = "vast.ai"
    elif "runpod" in hn_low:
        info["rental_provider"] = "runpod"
    elif "autodl" in hn_low or "container-fnubq" in hn_low:
        info["rental_provider"] = "autodl"
    elif "paperspace" in hn_low:
        info["rental_provider"] = "paperspace"
    # Power-cap writable probe: try a no-op set to current limit. If we
    # get "Insufficient Permissions" or similar, the host owns the cap.
    # We never actually CHANGE the power limit — just probe whether we could.
    info["power_cap_writable"] = _probe_power_cap_writable()
    return info


def _probe_power_cap_writable() -> bool:
    """Returns True iff `nvidia-smi -pl <current>` would succeed (i.e.,
    we have permission). Does NOT change the power cap — sets it to its
    own current value as a no-op probe. Errors out gracefully if
    nvidia-smi isn't present (CPU-only or non-NVIDIA hosts)."""
    try:
        import subprocess
        # Read current power limit
        cur = subprocess.run(
            ["nvidia-smi", "--query-gpu=power.limit", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if cur.returncode != 0 or not cur.stdout.strip():
            return False
        try:
            limit = int(float(cur.stdout.strip().split("\n")[0]))
        except (ValueError, IndexError):
            return False
        # No-op set
        probe = subprocess.run(
            ["nvidia-smi", "-i", "0", "-pl", str(limit)],
            capture_output=True, text=True, timeout=5,
        )
        return probe.returncode == 0
    except Exception:
        return False


def _collect_numa_topology(gpus: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Read NUMA topology from /sys/devices/system/node/ and map each GPU's
    PCIe device to its NUMA node. Returns None on non-NUMA hosts or when
    we can't read the sysfs view (no need to send empty data).
    """
    import os as _os
    import os.path as _op
    nodes = []
    node_dir = "/sys/devices/system/node"
    if not _op.isdir(node_dir):
        return None
    try:
        for entry in sorted(_os.listdir(node_dir)):
            if not entry.startswith("node") or not entry[4:].isdigit():
                continue
            node_id = int(entry[4:])
            try:
                with open(_op.join(node_dir, entry, "cpulist"), "r") as f:
                    cpus = f.read().strip()
            except Exception:
                cpus = ""
            nodes.append({"id": node_id, "cpu_range": cpus})
    except Exception:
        return None
    if len(nodes) <= 1:
        # Single-NUMA host — no pinning advice needed.
        return {"nodes": len(nodes), "topology": nodes, "gpu_numa_affinity": {}}

    # Map each GPU to its NUMA node via /sys/bus/pci/devices/<bdf>/numa_node.
    # We get the PCI BDF from nvidia-smi -q -d MEMORY (or the gpu_uuid → BDF
    # path), but the simplest is to walk /sys/bus/pci and match GPU class
    # 0x030200 (3D controller).
    gpu_affinity: dict[str, int] = {}
    try:
        for entry in _os.listdir("/sys/bus/pci/devices"):
            try:
                with open(_op.join("/sys/bus/pci/devices", entry, "class"), "r") as f:
                    cls = f.read().strip()
                # PCI classes: 0x030000 (VGA), 0x030200 (3D controller).
                # NVIDIA GPUs may register as either.
                if not (cls.startswith("0x0300") or cls.startswith("0x0302")):
                    continue
                with open(_op.join("/sys/bus/pci/devices", entry, "vendor"), "r") as f:
                    vendor = f.read().strip()
                # NVIDIA vendor ID is 0x10de
                if vendor != "0x10de":
                    continue
                with open(_op.join("/sys/bus/pci/devices", entry, "numa_node"), "r") as f:
                    node_raw = f.read().strip()
                node = int(node_raw)
                if node < 0:
                    # NUMA node -1 = unknown (single-NUMA or undefined).
                    continue
                # We can't easily match BDF → gpu_uuid here without parsing
                # nvidia-smi. Use the entry BDF as the key; pool can still
                # use a node count + an "any GPU's node" hint to drive
                # the numactl command.
                gpu_affinity[entry] = node
            except Exception:
                continue
    except Exception:
        pass
    # Also try the canonical mapping via nvidia-smi --query-gpu=uuid,pci_bus_id
    try:
        import subprocess
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=uuid,pci.bus_id", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            uuid_to_node = {}
            for line in out.stdout.strip().split("\n"):
                if "," not in line:
                    continue
                uuid, bus_id = (x.strip() for x in line.split(",", 1))
                # bus_id format: 00000000:01:00.0  →  /sys/bus/pci uses 0000:01:00.0
                norm = bus_id.lower()
                if len(norm) == len("00000000:01:00.0"):
                    norm = norm[4:]  # strip leading domain to match sysfs
                if norm in gpu_affinity:
                    uuid_to_node[uuid] = gpu_affinity[norm]
            if uuid_to_node:
                gpu_affinity = uuid_to_node
    except Exception:
        pass

    return {
        "nodes": len(nodes),
        "topology": nodes,
        "gpu_numa_affinity": gpu_affinity,
    }


def _probe_active_backend(solver_path: str | None) -> dict[str, Any]:
    """Probe the solver binary for its active backend + supported CUDA
    archs. Detects silent CPU fallback (B58-class) and binary/arch
    mismatch (e.g. running a sm_89-only binary on a Blackwell card).
    Returns {"active": "cuda"|"cpu"|None, "cuda_archs": "89,120"|None}.
    """
    result: dict[str, Any] = {"active": None, "cuda_archs": None}
    if not solver_path:
        return result
    import subprocess, os as _os, os.path as _op
    if not _op.isfile(solver_path) or not _os.access(solver_path, _os.X_OK):
        return result
    # Active backend: btx-matmul-backend-info preferred; fall back to
    # `--help` heuristics if not available.
    try:
        # Many builds ship a helper binary alongside btx-gbt-solve.
        helper = _op.join(_op.dirname(solver_path), "btx-matmul-backend-info")
        if _op.isfile(helper) and _os.access(helper, _os.X_OK):
            out = subprocess.run(
                [helper, "--backend", "cuda"],
                capture_output=True, text=True, timeout=10,
            )
            if out.returncode == 0:
                # Output is JSON typically. Look for "active_backend".
                import json as _json
                try:
                    data = _json.loads(out.stdout)
                    result["active"] = data.get("active_backend")
                except _json.JSONDecodeError:
                    # Plain-text fallback
                    for line in out.stdout.split("\n"):
                        if "active_backend" in line.lower():
                            parts = line.split(":", 1)
                            if len(parts) == 2:
                                result["active"] = parts[1].strip().strip('"')
                                break
    except Exception:
        pass
    # CUDA archs: peek at the binary via cuobjdump if available, otherwise
    # via embedded sm_NN strings (works for our HIP build too).
    try:
        out = subprocess.run(
            ["cuobjdump", "--list-elf", solver_path],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            import re
            archs = sorted(set(re.findall(r"sm_(\d+)", out.stdout)))
            if archs:
                result["cuda_archs"] = ",".join(archs)
    except Exception:
        pass
    # Last-resort: scan strings(1) for sm_NN and gfx markers
    if result["cuda_archs"] is None:
        try:
            out = subprocess.run(
                ["strings", solver_path],
                capture_output=True, text=True, timeout=15,
            )
            import re
            archs = sorted(set(re.findall(r"\bsm_(\d+)\b", out.stdout)))
            if archs:
                result["cuda_archs"] = ",".join(archs)
        except Exception:
            pass
    return result


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


_GPU_SAMPLE_COUNT = 4   # samples per metrics call
_GPU_SAMPLE_INTERVAL_S = 0.5   # ~2 s window total


def _gpu_runtime() -> list[dict[str, Any]]:
    """Per-GPU runtime metrics: util%, power_w, temp_c, uuid.

    Multi-samples nvidia-smi over a ~2 s window and averages util_pct + power_w.
    Single-snapshot is unsuited to bursty mining workloads — on Pascal post-fork
    the matmul scan kernel runs for ~3 s then the host iterates flags briefly;
    one instantaneous sample lands in the inter-scan gap ~80% of the time,
    underreporting both util and power. The 2 s window catches at least one
    in-kernel sample on any realistic mining cadence."""
    if platform.system() == "Darwin":
        # Match the static gpu_uuid so the dashboard associates this worker's
        # GPU. Per-GPU util/power on Apple Silicon needs `powermetrics` (root),
        # so report the identity with null metrics rather than nothing.
        gpus = _apple_gpus()
        if not gpus:
            return []
        return [{"gpu_uuid": gpus[0]["gpu_uuid"], "util_pct": None,
                 "power_w": None, "temp_c": None, "pstate": None,
                 "clocks_sm": None, "power_limit_w": None, "latched": False}]

    # Multi-sample NVIDIA over ~2 s. Index by uuid (multi-GPU safe), accumulate
    # util/power sums + sample counts, take the latest temp_c.
    accum: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    for i in range(_GPU_SAMPLE_COUNT):
        rows = _nvidia_query("uuid,utilization.gpu,power.draw,temperature.gpu,pstate,clocks.sm,power.max_limit")
        for r in rows:
            if len(r) < 4:
                continue
            uuid, util, power, temp = r[:4]
            pstate = r[4].strip() if len(r) > 4 else ""
            clk_sm = r[5] if len(r) > 5 else ""
            plimit = r[6] if len(r) > 6 else ""
            d = accum.setdefault(uuid, {"util_sum": 0.0, "util_n": 0,
                                        "power_sum": 0.0, "power_n": 0,
                                        "clk_sum": 0.0, "clk_n": 0,
                                        "temp_c": None, "pstate": None,
                                        "power_limit_w": None})
            try:
                d["util_sum"] += float(util); d["util_n"] += 1
            except ValueError:
                pass
            try:
                d["power_sum"] += float(power); d["power_n"] += 1
            except ValueError:
                pass
            try:
                d["clk_sum"] += float(clk_sm); d["clk_n"] += 1
            except ValueError:
                pass
            try:
                d["temp_c"] = int(float(temp))
            except ValueError:
                pass
            # pstate ("P0".."P12") + power limit are ~static per GPU; keep latest valid.
            if pstate and pstate not in ("[Not Supported]", "[N/A]"):
                d["pstate"] = pstate
            try:
                d["power_limit_w"] = round(float(plimit), 1)
            except ValueError:
                pass
            counts[uuid] = counts.get(uuid, 0) + 1
        if i < _GPU_SAMPLE_COUNT - 1:
            time.sleep(_GPU_SAMPLE_INTERVAL_S)

    out = []
    for uuid, d in accum.items():
        util_pct = int(d["util_sum"] / d["util_n"]) if d["util_n"] else None
        power_w = round(d["power_sum"] / d["power_n"], 1) if d["power_n"] else None
        clocks_sm = int(d["clk_sum"] / d["clk_n"]) if d["clk_n"] else None
        power_limit_w = d["power_limit_w"]
        # Latch detector (v0.4.20): a wedged GPU sits at high util but draws far
        # below its power ceiling — the clock/P-state collapsed. POWER is the
        # discriminator; util alone reads ~98% in BOTH healthy and latched states.
        # Flag util>=90% AND draw < 50% of the card's own max power limit.
        latched = bool(
            util_pct is not None and util_pct >= 90
            and power_w is not None and power_limit_w
            and power_w < 0.5 * power_limit_w
        )
        out.append({
            "gpu_uuid": uuid,
            "util_pct": util_pct,
            "power_w": power_w,
            "temp_c": d["temp_c"],
            "pstate": d["pstate"],
            "clocks_sm": clocks_sm,
            "power_limit_w": power_limit_w,
            "latched": latched,
        })

    if not out:
        # AMD/ROCm host: report the static uuid so the dashboard associates the
        # GPU. (Per-GPU util/power parsing from rocm-smi is left for later.)
        amd = _amd_gpus()
        if amd:
            out = [{"gpu_uuid": amd[0]["gpu_uuid"], "util_pct": None,
                    "power_w": None, "temp_c": None, "pstate": None,
                    "clocks_sm": None, "power_limit_w": None, "latched": False}]
    return out


def solver_sha256_hex(solver_path: str | None) -> str | None:
    """sha256 of the installed solver binary, or None if not readable.
    Used to populate the cohort field so the pool can group workers by the
    exact solver build they're running (canary vs stable A/B)."""
    if not solver_path:
        return None
    try:
        h = hashlib.sha256()
        with open(solver_path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def collect_runtime_metrics(
    session_id: str,
    solver_nps: float | None,
    shares_session_total: int,
    *,
    wrapper_version: str | None = None,
    solver_sha256: str | None = None,
    solver_backend: str | None = None,
) -> dict[str, Any]:
    """Build a `worker.report_metrics` params payload (sent every 60s).

    `solver_nps` is the miner's last-known nonces-per-second figure
    (self-reported by the solver). Caller passes None if unknown.

    `wrapper_version`, `solver_sha256`, `solver_backend` are the H2/H3 cohort
    keys (pool migration 20260611030000). They let `/api/fleet` group workers
    into canary-vs-stable cohorts by the exact build they're running. Older
    pool servers ignore the extra fields gracefully.
    """
    return {
        "session_id": session_id,
        "timestamp": int(time.time()),
        "cpu_util_pct": _cpu_util_pct(),
        "ram_gb_used": _ram_gb_used(),
        "gpus": _gpu_runtime(),
        "solver_nps": solver_nps,
        "shares_session_total": shares_session_total,
        "wrapper_version": wrapper_version,
        "solver_sha256": solver_sha256,
        "solver_backend": solver_backend,
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
