"""Central configuration. Everything overridable via environment variables so the
same code runs locally today and in a container on AWS/GCP later."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DOCBRAIN_HOME = Path(os.environ.get("DOCBRAIN_HOME", Path.home() / ".docbrain"))

# Confidence below this routes a table to the human-review queue.
REVIEW_THRESHOLD = float(os.environ.get("DOCBRAIN_REVIEW_THRESHOLD", "0.80"))

# LLM backend: "anthropic" (API key), "claude-cli" (subscription auth), "none".
LLM_BACKEND = os.environ.get("DOCBRAIN_LLM", "auto")
LLM_MODEL = os.environ.get("DOCBRAIN_MODEL", "claude-opus-5")

# Agent refinement: "auto" (only ambiguous tables), "always", "never".
AGENT_MODE = os.environ.get("DOCBRAIN_AGENT", "auto")

SANDBOX_TIMEOUT = int(os.environ.get("DOCBRAIN_SANDBOX_TIMEOUT", "120"))

# PDF page classification thresholds (pdf-inspector pattern: cheap text/image
# heuristics per page, GPU/VLM only for pages that need it).
PDF_TEXT_MIN_CHARS = 80
PDF_IMAGE_COVERAGE_SCANNED = 0.5


@dataclass
class Paths:
    home: Path = field(default_factory=lambda: DOCBRAIN_HOME)

    @property
    def catalog(self) -> Path:
        return self.home / "catalog.duckdb"

    def project_dir(self, project: str) -> Path:
        return self.home / "projects" / project

    def tables_dir(self, project: str) -> Path:
        return self.project_dir(project) / "tables"

    def sketches_dir(self, project: str) -> Path:
        return self.project_dir(project) / "sketches"

    def context_path(self, project: str) -> Path:
        return self.project_dir(project) / "context.md"

    @property
    def scripts_dir(self) -> Path:
        # Curated agent-authored extraction scripts. Global, not per-project:
        # a parser for a format is knowledge, not project data.
        d = self.home / "scripts"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def ensure(self, project: str) -> None:
        self.tables_dir(project).mkdir(parents=True, exist_ok=True)
        self.sketches_dir(project).mkdir(parents=True, exist_ok=True)


PATHS = Paths()
