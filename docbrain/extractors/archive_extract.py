"""Zip archives: not a format of their own — a container for other formats.

Extracted into a per-doc scratch dir and each entry routed through the SAME
detect_type()/ingest machinery as a top-level file, recursively (an entry can
itself be an xlsx, csv, pdf, txt, or another zip). Every entry becomes its own
document with source_ref noting "<archive>::<entry path>" for provenance, and
the archive itself is recorded as a container with links to its children.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

MAX_ENTRIES = 500
MAX_TOTAL_BYTES = 500 * 1024 * 1024  # zip-bomb guard


def list_entries(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as z:
        return [n for n in z.namelist() if not n.endswith("/")]


def extract_entries(path: Path, out_dir: Path) -> list[Path]:
    """Extract every file entry to out_dir (flattened per-entry subfolders to
    dodge name collisions), skipping unsafe paths. Returns extracted paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    with zipfile.ZipFile(path) as z:
        infos = [i for i in z.infolist() if not i.is_dir()][:MAX_ENTRIES]
        total = sum(i.file_size for i in infos)
        if total > MAX_TOTAL_BYTES:
            raise ValueError(f"archive expands to {total} bytes — over the "
                             f"{MAX_TOTAL_BYTES} bomb guard, skipping")
        for i, info in enumerate(infos):
            name = info.filename
            if name.startswith("/") or ".." in Path(name).parts:
                continue  # zip-slip guard
            entry_dir = out_dir / f"e{i}"
            entry_dir.mkdir(parents=True, exist_ok=True)
            dest = entry_dir / Path(name).name
            with z.open(info) as src, open(dest, "wb") as dst:
                dst.write(src.read())
            out.append(dest)
    return out
