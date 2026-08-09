"""Code-execution sandbox for agent-written Python (the SheetAgent/SheetBrain
execution surface, local edition).

Isolation, best available locally:
- macOS: `sandbox-exec` with a deny-network profile + writes confined to the
  job directory. (Deprecated by Apple but functional; good enough for v0.)
- fallback: plain subprocess with rlimits (CPU/file-size), minimal env,
  isolated interpreter (-I), read-only input copies. NOTE: no network denial
  in fallback mode — the run result records which mode executed.

Swap point for cloud: replace this class with a Firecracker/Docker executor;
the run_python() contract (inputs -> code -> OUT/*.parquet + stdout) is stable.
"""

from __future__ import annotations

import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import SANDBOX_TIMEOUT

PREAMBLE = """\
import json, sys
from pathlib import Path
IN = Path("in")     # read-only copies of input files
OUT = Path("out")   # write extracted tables here as .parquet
OUT.mkdir(exist_ok=True)
"""

SBPL_PROFILE = """\
(version 1)
(allow default)
(deny network*)
(deny file-write*)
(allow file-write* (subpath "{workdir}"))
(allow file-write* (literal "/dev/null"))
(allow file-write* (literal "/dev/dtracehelper"))
"""


@dataclass
class RunResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int
    outputs: list[Path] = field(default_factory=list)
    workdir: Path | None = None
    mode: str = "subprocess"


class Sandbox:
    def __init__(self, timeout: int = SANDBOX_TIMEOUT):
        self.timeout = timeout
        self.mode = self._pick_mode()

    def _pick_mode(self) -> str:
        if platform.system() == "Darwin" and shutil.which("sandbox-exec"):
            probe = self._exec("print('ok')", {}, mode="sandbox-exec", quick=True)
            if probe.ok and "ok" in probe.stdout:
                return "sandbox-exec"
        return "subprocess"

    def run_python(self, code: str, inputs: dict[str, Path]) -> RunResult:
        return self._exec(code, inputs, mode=self.mode)

    def _exec(self, code: str, inputs: dict[str, Path], mode: str, quick: bool = False) -> RunResult:
        workdir = Path(tempfile.mkdtemp(prefix="docbrain-sbx-"))
        in_dir = workdir / "in"
        out_dir = workdir / "out"
        in_dir.mkdir()
        out_dir.mkdir()
        for name, src in inputs.items():
            dst = in_dir / name
            shutil.copy2(src, dst)
            dst.chmod(0o444)
        job = workdir / "job.py"
        job.write_text(PREAMBLE + code)

        argv = [sys.executable, "-B", "-I", "job.py"]
        if mode == "sandbox-exec":
            profile = SBPL_PROFILE.format(workdir=str(workdir.resolve()))
            argv = ["sandbox-exec", "-p", profile] + argv

        env = {
            "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
            "HOME": str(workdir),
            "TMPDIR": str(workdir),
            "MPLCONFIGDIR": str(workdir),
        }
        timeout = 15 if quick else self.timeout

        def limits():
            resource.setrlimit(resource.RLIMIT_CPU, (timeout, timeout))
            resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024 * 1024,) * 2)

        try:
            proc = subprocess.run(argv, cwd=workdir, env=env, capture_output=True,
                                  text=True, timeout=timeout + 15, preexec_fn=limits)
            stdout, stderr, rc = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired:
            return RunResult(False, "", f"timed out after {timeout}s", -1,
                             workdir=workdir, mode=mode)
        outputs = sorted(p for p in out_dir.rglob("*") if p.is_file())
        return RunResult(rc == 0, stdout[-8000:], stderr[-4000:], rc,
                         outputs=outputs, workdir=workdir, mode=mode)

    def probe_network_denied(self) -> bool:
        """True when the sandbox provably blocks network egress."""
        code = ("import socket\n"
                "try:\n"
                "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
                "    print('NET_OPEN')\n"
                "except Exception as e:\n"
                "    print('NET_BLOCKED', type(e).__name__)\n")
        res = self._exec(code, {}, mode=self.mode, quick=True)
        return "NET_BLOCKED" in res.stdout


def load_manifest(res: "RunResult") -> dict | None:
    """The standardized output contract for agent-run extraction code:
    OUT/manifest.json declaring format_id + tables (path/name/description/
    columns). Returns the validated manifest or None. Tables whose parquet is
    missing or unreadable are dropped from the manifest (recorded under
    "_invalid"), so callers can trust every surviving entry."""
    if res.workdir is None:
        return None
    mpath = res.workdir / "out" / "manifest.json"
    if not mpath.exists():
        return None
    try:
        manifest = json.loads(mpath.read_text())
    except json.JSONDecodeError:
        return None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("tables"), list):
        return None
    import pandas as pd
    valid, invalid = [], []
    for t in manifest["tables"]:
        rel = str(t.get("path", ""))
        p = (res.workdir / "out" / rel).resolve()
        if not str(p).startswith(str((res.workdir / "out").resolve())) or not p.exists():
            invalid.append({"path": rel, "error": "missing or outside OUT/"})
            continue
        try:
            df = pd.read_parquet(p)
        except Exception as e:
            invalid.append({"path": rel, "error": str(e)[:200]})
            continue
        if df.empty:
            invalid.append({"path": rel, "error": "empty table"})
            continue
        t["_abs_path"] = str(p)
        t["_shape"] = list(df.shape)
        valid.append(t)
    manifest["tables"] = valid
    manifest["_invalid"] = invalid
    return manifest


def describe_outputs(outputs: list[Path]) -> list[dict]:
    """Load head/schema of parquet outputs so the agent can inspect its own work."""
    import pandas as pd
    out = []
    for p in outputs:
        if p.suffix != ".parquet":
            continue
        try:
            df = pd.read_parquet(p)
            out.append({
                "path": p.name,
                "shape": list(df.shape),
                "columns": [{"name": str(c), "dtype": str(df[c].dtype)} for c in df.columns],
                "head": json.loads(df.head(5).to_json(orient="records", date_format="iso")),
            })
        except Exception as e:
            out.append({"path": p.name, "error": str(e)})
    return out
