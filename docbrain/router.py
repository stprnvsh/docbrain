"""Format router — content-based type detection (never trust extensions alone).
First stage of the pipeline: file in -> format/quality classification -> track."""

from __future__ import annotations

import zipfile
from pathlib import Path

SUPPORTED = {"xlsx", "pdf", "csv"}
PLANNED = {"docx", "pptx"}


def detect_type(path: Path) -> str:
    """Returns xlsx | pdf | csv | docx | pptx | unknown."""
    head = path.open("rb").read(8)
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        # OOXML container — look inside to distinguish xlsx/docx/pptx.
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist()[:50])
                joined = " ".join(names)
                if "xl/workbook.xml" in names or "xl/" in joined:
                    return "xlsx"
                if "word/" in joined:
                    return "docx"
                if "ppt/" in joined:
                    return "pptx"
        except zipfile.BadZipFile:
            return "unknown"
        return "unknown"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "xls-legacy"
    ext = path.suffix.lower().lstrip(".")
    # Known-but-unsupported types get honest labels so the context brain can
    # still tell the user what exists in the project.
    if ext == "mtx":
        return "visum-matrix"
    if ext in {"shp", "shx", "dbf", "prj", "cpg", "ctf", "qix", "sbn", "sbx"}:
        return "shapefile-part"
    if ext == "zip":
        return "archive"
    if ext in {"csv", "tsv", "txt"}:
        return "csv"
    # Fall back: does it look like delimited text?
    try:
        sample = path.open("rb").read(4096)
        sample.decode("utf-8", errors="strict")
        return "csv" if ext == "" or ext in {"csv", "tsv"} else "unknown"
    except UnicodeDecodeError:
        pass
    if ext in SUPPORTED | PLANNED:
        return ext
    return "unknown"
