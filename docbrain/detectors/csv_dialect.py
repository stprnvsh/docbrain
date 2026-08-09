"""CSV track, stage 1: encoding sniff + dialect detection + block splitting.

- Encoding: charset-normalizer with latin-1/cp1252 fallbacks (the common
  real-world alternates when UTF-8 fails).
- Dialect: CleverCSV's consistency-measure detection, falling back to the
  stdlib sniffer, falling back to comma.
- Multi-table-in-one-CSV: stacked tables separated by blank lines are split
  into blocks (same gap-segmentation idea as the xlsx island detector).
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

from charset_normalizer import from_bytes

try:
    import clevercsv
    HAVE_CLEVERCSV = True
except ImportError:  # pragma: no cover
    HAVE_CLEVERCSV = False


@dataclass
class CsvBlock:
    index: int
    text: str
    start_line: int
    n_lines: int


@dataclass
class CsvSniff:
    encoding: str
    delimiter: str
    quotechar: str
    blocks: list[CsvBlock] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """Returns (text, encoding_name).

    UTF-8 first. On failure: mostly-ASCII bytes with sprinkled high bytes are the
    signature of western business exports, where cp1252 is the overwhelmingly
    common truth and statistical detectors routinely mis-guess (cp775/cp850 on
    short files). Dense non-ASCII content goes to charset-normalizer instead."""
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    non_ascii = sum(b >= 0x80 for b in raw) / max(len(raw), 1)
    if non_ascii < 0.2:
        try:
            return raw.decode("cp1252"), "cp1252"
        except UnicodeDecodeError:
            pass
    best = from_bytes(raw).best()
    if best is not None:
        return str(best), best.encoding
    for enc in ("cp1252", "latin-1"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


def sniff_dialect(text: str) -> tuple[str, str, str]:
    """Returns (delimiter, quotechar, method)."""
    sample = text[:64_000]
    if HAVE_CLEVERCSV:
        try:
            dialect = clevercsv.Sniffer().sniff(sample)
            if dialect is not None and dialect.delimiter:
                return dialect.delimiter, dialect.quotechar or '"', "clevercsv"
        except Exception:
            pass
    try:
        d = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        return d.delimiter, d.quotechar or '"', "stdlib"
    except csv.Error:
        return ",", '"', "default"


def split_blocks(text: str) -> list[CsvBlock]:
    """Split on runs of >=1 fully blank line, keeping only blocks that look tabular
    (>=2 lines). Single-block files come back as one block."""
    lines = text.splitlines()
    blocks: list[CsvBlock] = []
    cur: list[str] = []
    start = 0
    for i, line in enumerate(lines):
        if line.strip() == "":
            if cur:
                blocks.append(CsvBlock(len(blocks), "\n".join(cur), start, len(cur)))
                cur = []
        else:
            if not cur:
                start = i
            cur.append(line)
    if cur:
        blocks.append(CsvBlock(len(blocks), "\n".join(cur), start, len(cur)))
    tabular = [b for b in blocks if b.n_lines >= 2]
    if len(tabular) >= 2:
        return tabular
    # Single logical table: return everything as one block (blank lines inside are noise).
    return [CsvBlock(0, text, 0, len(lines))]


def sniff_file(path: Path) -> CsvSniff:
    raw = path.read_bytes()
    text, encoding = decode_bytes(raw)
    delim, quote, method = sniff_dialect(text)
    sniff = CsvSniff(encoding=encoding, delimiter=delim, quotechar=quote,
                     blocks=split_blocks(text))
    sniff.notes.append(f"dialect via {method}")
    if encoding not in ("utf-8", "ascii"):
        sniff.notes.append(f"non-utf8 encoding: {encoding}")
    if len(sniff.blocks) > 1:
        sniff.notes.append(f"{len(sniff.blocks)} stacked blocks detected")
    return sniff


def read_block(block: CsvBlock, delimiter: str, quotechar: str):
    """Parse one block into rows + collect malformed (ragged) row indices."""
    reader = csv.reader(io.StringIO(block.text), delimiter=delimiter, quotechar=quotechar)
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return [], []
    from collections import Counter
    width = Counter(len(r) for r in rows).most_common(1)[0][0]
    bad = [i for i, r in enumerate(rows) if len(r) != width]
    return rows, bad
