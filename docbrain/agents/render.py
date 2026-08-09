"""Vision-fallback rendering: turn a region of a document into an image the
model can look at. One primitive, two callers (xlsx ambiguous ranges, PDF
pages) — the SpreadsheetAgent/Docling pattern."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def render_grid_png(grid: list[list], out_path: Path, title: str = "") -> Path:
    """Render a raw cell grid (xlsx island) as a table image with gridlines,
    so the model can reason about layout the way a human would."""
    rows = [[("" if v is None else str(v))[:28] for v in r] for r in grid[:30]]
    if not rows:
        rows = [[""]]
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    fig_w = min(1.4 * ncols + 1, 22)
    fig_h = min(0.42 * len(rows) + 1, 18)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))
    ax.axis("off")
    table = ax.table(cellText=rows, loc="center", cellLoc="left")
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1, 1.3)
    if title:
        ax.set_title(title, fontsize=10)
    fig.tight_layout()
    fig.savefig(out_path, dpi=130)
    plt.close(fig)
    return out_path
