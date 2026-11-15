from __future__ import annotations

import os
import sys
from pathlib import Path


_DLL_DIRECTORY_HANDLES: list[object] = []


def prepare_windows_cuda_dll_search_path() -> list[str]:
    """Register existing CUDA/CTranslate2 DLL folders before native inference loads."""
    if os.name != "nt":
        return []

    candidates: list[Path] = []
    for variable in ("CUDA_PATH", "CUDA_PATH_V12_8", "CUDA_PATH_V12_6", "CUDA_PATH_V12_4", "CUDA_PATH_V12_3"):
        root = os.environ.get(variable)
        if root:
            candidates.append(Path(root) / "bin")

    site_packages = Path(sys.prefix) / "Lib" / "site-packages"
    candidates.extend((
        site_packages / "ctranslate2",
        site_packages / "nvidia" / "cublas" / "bin",
        site_packages / "nvidia" / "cublas" / "lib",
        site_packages / "nvidia" / "cudnn" / "bin",
        site_packages / "nvidia" / "cudnn" / "lib",
    ))

    registered: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        normalized = str(candidate.resolve()).casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(normalized))
        registered.append(normalized)

    if registered:
        os.environ["PATH"] = os.pathsep.join(registered + [os.environ.get("PATH", "")])
    return registered
