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
    # Non-table content (label:value preambles, stray fragments) — preserved
    # as text for the context layer, never silently discarded.
    preambles: list[str] = field(default_factory=list)


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


def _looks_like_label_preamble(block_text: str, delimiter: str,
                               max_lines: int = 8, min_ratio: float = 0.6) -> bool:
    """True for a `Label:;value;` metadata block (e.g. "Zählbeginn:;...;"),
    the specific shape that precedes many exported count files. Targets the
    exact pattern (colon-terminated first field, small line count) rather
    than a general "not tabular" judgment — a real second table, however
    small or text-heavy (a vehicle-class legend, a code lookup), does NOT
    have this shape and is never affected. See split_blocks."""
    lines = [l for l in block_text.splitlines() if l.strip()]
    if not lines or len(lines) > max_lines:
        return False
    colon_labels = sum(1 for line in lines
                       if (line.split(delimiter, 1)[0]).strip().endswith(":"))
    return colon_labels / len(lines) >= min_ratio


def split_blocks(text: str, delimiter: str | None = None
                 ) -> tuple[list[CsvBlock], list[str]]:
    """Split on runs of >=1 fully blank line. Returns (table_blocks, preambles).

    Design rule for randomly-structured (government) data: PRESERVE STRUCTURE,
    never collapse it. However many sub-tables the file has, that many blocks
    come back — 7 blocks means 7 candidates. The only demotion is the specific
    label:value preamble shape (see _looks_like_label_preamble) and stray
    single-line fragments, and those are returned as text — kept for the
    context layer, not discarded. Whole-file re-gluing happens ONLY when the
    file never had blank-line structure to begin with; once real structure is
    found, surviving blocks are returned as-is even if only one survives."""
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

    if len(blocks) <= 1:
        return [CsvBlock(0, text, 0, len(lines))], []

    preambles: list[str] = []
    tabular: list[CsvBlock] = []
    for b in blocks:
        if b.n_lines < 2:
            preambles.append(b.text)  # stray fragment (title, footnote)
        elif delimiter and _looks_like_label_preamble(b.text, delimiter):
            preambles.append(b.text)
        else:
            tabular.append(b)
    if tabular:
        return tabular, preambles
    # Everything demoted (pure metadata file): keep it whole as one candidate.
    return [CsvBlock(0, text, 0, len(lines))], []


def sniff_file(path: Path) -> CsvSniff:
    raw = path.read_bytes()
    text, encoding = decode_bytes(raw)
    delim, quote, method = sniff_dialect(text)
    table_blocks, preambles = split_blocks(text, delimiter=delim)
    sniff = CsvSniff(encoding=encoding, delimiter=delim, quotechar=quote,
                     blocks=table_blocks, preambles=preambles)
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
