"""LLM client with swappable backends.

- anthropic  : official SDK (ANTHROPIC_API_KEY or `ant auth login` profile).
- claude-cli : headless `claude -p` — uses the local Claude Code subscription
               auth, so the pipeline runs with zero API-key setup. Vision works
               by letting the CLI Read image files (read-only tool allowlist).
- none       : heuristics-only mode; every LLM-dependent step degrades to
               flags + human-review queue.

Swap point for cloud: an AWS deployment sets DOCBRAIN_LLM=anthropic and points
the SDK at Bedrock/first-party; nothing else changes.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .config import LLM_BACKEND, LLM_MODEL


class LLMError(RuntimeError):
    pass


def detect_backend() -> str:
    if LLM_BACKEND != "auto":
        return LLM_BACKEND
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if shutil.which("claude"):
        return "claude-cli"
    return "none"


class LLM:
    def __init__(self, backend: str | None = None, model: str | None = None):
        self.backend = backend or detect_backend()
        self.model = model or LLM_MODEL
        self._client = None
        if self.backend == "anthropic":
            import anthropic
            self._client = anthropic.Anthropic()

    @property
    def available(self) -> bool:
        return self.backend != "none"

    @property
    def supports_vision(self) -> bool:
        return self.backend in ("anthropic", "claude-cli")

    # ------------------------------------------------------------------
    def complete(self, prompt: str, system: str | None = None,
                 images: list[Path] | None = None, max_tokens: int = 4000,
                 timeout: int = 600) -> str:
        if self.backend == "anthropic":
            return self._anthropic(prompt, system, images, max_tokens)
        if self.backend == "claude-cli":
            return self._claude_cli(prompt, system, images, timeout)
        raise LLMError("no LLM backend available (set ANTHROPIC_API_KEY or install claude CLI)")

    def complete_json(self, prompt: str, system: str | None = None,
                      images: list[Path] | None = None, max_tokens: int = 4000):
        raw = None
        for attempt, delay in enumerate((0, 3, 10)):
            if delay:
                time.sleep(delay)
            try:
                raw = self.complete(prompt + "\n\nReturn ONLY valid JSON, no prose.",
                                    system, images, max_tokens)
                break
            except LLMError:
                if attempt == 2:
                    raise
        try:
            return extract_json(raw)
        except ValueError:
            retry = self.complete(
                "Your previous reply was not valid JSON. Reply again with ONLY the JSON.\n\n"
                f"Previous reply:\n{raw[:2000]}", system, None, max_tokens)
            return extract_json(retry)

    # ------------------------------------------------------------------
    def _anthropic(self, prompt, system, images, max_tokens) -> str:
        content: list[dict] = []
        for img in images or []:
            data = base64.standard_b64encode(Path(img).read_bytes()).decode()
            media = "image/png" if str(img).lower().endswith("png") else "image/jpeg"
            content.append({"type": "image",
                            "source": {"type": "base64", "media_type": media, "data": data}})
        content.append({"type": "text", "text": prompt})
        kwargs = dict(model=self.model, max_tokens=max_tokens,
                      messages=[{"role": "user", "content": content}])
        if system:
            kwargs["system"] = system
        resp = self._client.messages.create(**kwargs)
        if resp.stop_reason == "refusal":
            raise LLMError("model declined the request (refusal)")
        return "".join(b.text for b in resp.content if b.type == "text")

    def _claude_cli(self, prompt, system, images, timeout) -> str:
        full = prompt
        if images:
            paths = "\n".join(str(Path(p).resolve()) for p in images)
            full = ("First use the Read tool to view these image file(s), then answer:\n"
                    f"{paths}\n\n{prompt}")
        cmd = ["claude", "-p", "--output-format", "json"]
        if os.environ.get("DOCBRAIN_MODEL"):
            cmd += ["--model", self.model]  # otherwise the CLI's own default applies
        if system:
            cmd += ["--append-system-prompt", system]
        if images:
            cmd += ["--allowed-tools", "Read", "--max-turns", "6"]
        else:
            cmd += ["--max-turns", "2"]
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        try:
            proc = subprocess.run(cmd, input=full, capture_output=True, text=True,
                                  timeout=timeout, env=env)
        except subprocess.TimeoutExpired as e:
            raise LLMError(f"claude CLI timed out after {timeout}s") from e
        if proc.returncode != 0:
            raise LLMError(f"claude CLI failed rc={proc.returncode}: {proc.stderr[:500]}")
        try:
            payload = json.loads(proc.stdout)
            result = payload.get("result", "")
        except json.JSONDecodeError:
            result = proc.stdout
        if not result.strip():
            raise LLMError("claude CLI returned empty result")
        return result


def extract_json(raw: str):
    """Pull the first JSON object/array out of a model reply (handles fences)."""
    fence = re.search(r"```(?:json)?\s*(.+?)```", raw, re.DOTALL)
    if fence:
        raw = fence.group(1)
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = raw.find(opener)
        end = raw.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"no JSON found in reply: {raw[:200]!r}")
