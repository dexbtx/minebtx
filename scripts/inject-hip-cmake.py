#!/usr/bin/env python3
"""Inject the experimental HIP/ROCm backend wiring into upstream v0.32.2
src/CMakeLists.txt at build time (ROCm CI). Upstream has no HIP backend; the
HIP path reuses the cuda/ header interface (so it presents as backend "cuda")
and is built from cuda/ sources hipified at build time into hip/.

Idempotent-ish: refuses to double-inject. Anchors on stable text, not line
numbers, so it survives minor upstream drift.
"""
from __future__ import annotations
import sys
from pathlib import Path

HIP_OPTION_BLOCK = """
# ---- BTX experimental HIP/ROCm backend (AMD GPUs) — injected by CI ----------
option(BTX_ENABLE_HIP_EXPERIMENTAL "Enable experimental HIP/ROCm backend for AMD GPUs (mutually exclusive with CUDA)" OFF)
set(BTX_HIP_ARCHITECTURES "" CACHE STRING "Semicolon-separated AMD GPU archs (e.g. gfx1030;gfx1100;gfx1101)")
if(BTX_ENABLE_CUDA_EXPERIMENTAL AND BTX_ENABLE_HIP_EXPERIMENTAL)
  message(FATAL_ERROR "BTX_ENABLE_CUDA_EXPERIMENTAL and BTX_ENABLE_HIP_EXPERIMENTAL are mutually exclusive.")
endif()
if(BTX_ENABLE_HIP_EXPERIMENTAL)
  if(BTX_HIP_ARCHITECTURES STREQUAL "")
    message(FATAL_ERROR "BTX_ENABLE_HIP_EXPERIMENTAL=ON requires BTX_HIP_ARCHITECTURES (e.g. gfx1030;gfx1100).")
  endif()
  if(NOT DEFINED CMAKE_HIP_COMPILER AND NOT DEFINED CMAKE_HIP_PLATFORM)
    set(CMAKE_HIP_PLATFORM amd)
  endif()
  include(CheckLanguage)
  check_language(HIP)
  if(NOT CMAKE_HIP_COMPILER)
    message(FATAL_ERROR "BTX_ENABLE_HIP_EXPERIMENTAL=ON requires hipcc (ROCm 6.2+).")
  endif()
  enable_language(HIP)
  find_package(hip REQUIRED)
endif()
# ----------------------------------------------------------------------------
"""

ADD_LIB_ANCHOR = "add_library(btx_matmul_backend STATIC EXCLUDE_FROM_ALL"

# The exact upstream cuda-stub else-block we splice an `elseif(HIP)` in front of.
CUDA_STUB_BLOCK = """else()
  target_sources(btx_matmul_backend PRIVATE
    cuda/matmul_accel_stub.cpp
    cuda/oracle_accel_stub.cpp
  )
endif()"""

HIP_BRANCH = """elseif(BTX_ENABLE_HIP_EXPERIMENTAL)
  # HIP/ROCm backend (AMD). Hipified from cuda/ at build time; implements the
  # same btx::cuda:: interface, so it presents to backend_capabilities as "cuda".
  target_sources(btx_matmul_backend PRIVATE
    hip/cuda_context.cpp
    hip/matmul_accel.hip
    hip/oracle_accel.hip
  )
  set_source_files_properties(hip/matmul_accel.hip hip/oracle_accel.hip PROPERTIES LANGUAGE HIP)
  target_compile_definitions(btx_matmul_backend PRIVATE BTX_ENABLE_CUDA_EXPERIMENTAL=1 __HIP_PLATFORM_AMD__=1)
  target_link_libraries(btx_matmul_backend PRIVATE hip::host)
  set_target_properties(btx_matmul_backend PROPERTIES
    HIP_ARCHITECTURES "${BTX_HIP_ARCHITECTURES}"
    HIP_STANDARD 20
    HIP_STANDARD_REQUIRED ON)
else()
  target_sources(btx_matmul_backend PRIVATE
    cuda/matmul_accel_stub.cpp
    cuda/oracle_accel_stub.cpp
  )
endif()"""


# GCC-only hardening flags that clang/hipcc rejects (CUDA/nvcc tolerates them).
# We skip them for HIP builds — guarded by the -D cache var which is set on the
# cmake command line, so it's defined before these top-level lines run.
GCC_ONLY_FLAG_LINES = [
    'try_append_cxx_flags("-fno-extended-identifiers" TARGET core_interface SKIP_LINK)',
    'try_append_cxx_flags("-fstack-reuse=none" TARGET core_interface)',
]


def _patch_src_cmake(p: Path) -> None:
    txt = p.read_text()
    if "BTX_ENABLE_HIP_EXPERIMENTAL" in txt:
        print(f"{p}: HIP backend wiring already present.")
        return
    if ADD_LIB_ANCHOR not in txt:
        raise SystemExit(f"ERROR: anchor not found in {p}: {ADD_LIB_ANCHOR}")
    if CUDA_STUB_BLOCK not in txt:
        raise SystemExit(f"ERROR: cuda stub-else block not found in {p}")
    txt = txt.replace(ADD_LIB_ANCHOR, HIP_OPTION_BLOCK + "\n" + ADD_LIB_ANCHOR, 1)
    txt = txt.replace(CUDA_STUB_BLOCK, HIP_BRANCH, 1)
    p.write_text(txt)
    print(f"{p}: injected HIP option block + elseif(BTX_ENABLE_HIP_EXPERIMENTAL) branch.")


def _patch_top_cmake(p: Path) -> None:
    """Guard GCC-only hardening flags so they don't reach the HIP/clang compile."""
    txt = p.read_text()
    changed = 0
    for line in GCC_ONLY_FLAG_LINES:
        if line not in txt:
            print(f"WARN: GCC-only flag line not found (upstream drift?): {line}")
            continue
        guarded = f"if(NOT BTX_ENABLE_HIP_EXPERIMENTAL)\n  {line}\nendif()"
        if guarded in txt:
            continue
        txt = txt.replace(line, guarded, 1)
        changed += 1
    if changed:
        p.write_text(txt)
    print(f"{p}: guarded {changed} GCC-only flag(s) for non-HIP builds.")


def main() -> int:
    # Accept either a repo root or the src/CMakeLists.txt path (back-compat).
    arg = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    if arg.name == "CMakeLists.txt":
        root = arg.parent.parent if arg.parent.name == "src" else arg.parent
    else:
        root = arg
    _patch_src_cmake(root / "src" / "CMakeLists.txt")
    _patch_top_cmake(root / "CMakeLists.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
