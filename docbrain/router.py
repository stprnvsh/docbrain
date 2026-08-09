"""Format router — content-based type detection (never trust extensions alone).
First stage of the pipeline: file in -> format/quality classification -> track."""

from __future__ import annotations

import zipfile
from pathlib import Path

SUPPORTED = {"xlsx", "pdf", "csv", "txt", "office"}
# Office/e-book formats parse via the anydoc track when installed.
OFFICE_EXTS = {"doc", "docx", "odt", "rtf", "epub", "ppt", "pptx", "ods", "odp"}
PLANNED: set[str] = set()


def detect_type(path: Path) -> str:
    """Returns xlsx | pdf | csv | txt | office | <label> | unknown."""
    head = path.open("rb").read(8)
    ext = path.suffix.lower().lstrip(".")
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        # A plain zip archive and an OOXML/ODF document share this exact
        # magic (OOXML *is* a zip). Peek inside: office-shaped paths win;
        # anything else is a generic archive to explore, not "unknown" —
        # this is the only reachable path for .zip, since its extension
        # check further down is otherwise unreachable from here.
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist()[:50])
                joined = " ".join(names)
                if "xl/workbook.xml" in names or "xl/" in joined:
                    return "xlsx"
                if "word/" in joined or "ppt/" in joined or "mimetype" in names \
                        or ext in OFFICE_EXTS:
                    return "office"
        except zipfile.BadZipFile:
            return "unknown"
        return "archive"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        # Legacy CFB container: .doc/.ppt are office; .xls stays unsupported.
        return "office" if ext in OFFICE_EXTS else "xls-legacy"
    if ext in OFFICE_EXTS:
        return "office"
    # Known-but-unsupported types get honest labels so the context brain can
    # still tell the user what exists in the project.
    if ext == "mtx":
        return "visum-matrix"
    if ext in {"shp", "shx", "dbf", "prj", "cpg", "ctf", "qix", "sbn", "sbx"}:
        return "shapefile-part"
    if ext == "zip":
        return "archive"
    if ext in {"csv", "tsv"}:
        return "csv"
    if ext in {"txt", "log", "text", "dat"}:
        return "txt"
    # Extension unknown: decodable text goes to the txt track (which triages
    # further into delimited / records / prose).
    try:
        sample = path.open("rb").read(4096)
        sample.decode("utf-8", errors="strict")
        return "txt" if ext == "" else "unknown"
    except UnicodeDecodeError:
        pass
    if ext in SUPPORTED | PLANNED:
        return ext
    return "unknown"
