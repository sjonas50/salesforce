"""Bridge to the grammar-backed Apex parser (AD-31, grammar path).

Salesforce's own ANTLR grammar ships as the Node package
``@apexdevtools/apex-parser``; ``tools/apex-parser/apex_ast.js`` wraps it as a
persistent line-protocol server that emits one compact JSON tree per request.
This module owns the subprocess: one server per Python process, started on
first use, restarted if it dies. When Node or the package is missing the
analyzer falls back to the tokenizer, so nothing here is required at runtime.

Tree encoding: rule nodes are ``["RuleName", child, ...]`` lists, terminals are
strings; punctuation is dropped; SOQL/SOSL literals arrive verbatim as
``["SoqlLiteral", "[SELECT ...]"]``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from offramp.core.logging import get_logger

log = get_logger(__name__)

Node = list[Any]

_ENV_DIR = "OFFRAMP_APEX_PARSER_DIR"
_ENV_NODE = "OFFRAMP_NODE"


class AstUnavailable(RuntimeError):
    """Node or the parser package cannot be found."""


class AstError(RuntimeError):
    """The parser server failed on a request (not a syntax error in the source)."""


@dataclass
class AstResult:
    tree: Node
    errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def parser_dir() -> Path:
    """``tools/apex-parser`` in the repo, or ``$OFFRAMP_APEX_PARSER_DIR``."""
    override = os.environ.get(_ENV_DIR)
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().parents[4] / "tools" / "apex-parser"


def node_binary() -> str | None:
    return os.environ.get(_ENV_NODE) or shutil.which("node")


def is_available() -> bool:
    """True when ``node`` and the installed grammar package can be found."""
    d = parser_dir()
    return (
        node_binary() is not None
        and (d / "apex_ast.js").is_file()
        and (d / "node_modules" / "@apexdevtools" / "apex-parser").is_dir()
    )


class ApexAstServer:
    """One long-lived ``node apex_ast.js`` process with a request/response lock."""

    def __init__(self, directory: Path | None = None, node: str | None = None) -> None:
        self.directory = directory or parser_dir()
        self.node = node or node_binary() or "node"
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._seq = 0

    def _start(self) -> subprocess.Popen[str]:
        script = self.directory / "apex_ast.js"
        if not is_available() and not script.is_file():
            raise AstUnavailable(f"apex parser not installed under {self.directory}")
        proc = subprocess.Popen(
            [self.node, str(script)],
            cwd=str(self.directory),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        log.debug("apex.ast.server_started", pid=proc.pid, node=self.node)
        return proc

    def parse(self, source: str, kind: str = "auto") -> AstResult:
        """Parse one class/trigger body; ``kind`` is ``class``, ``trigger`` or ``auto``."""
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                self._proc = self._start()
            self._seq += 1
            req = {"id": str(self._seq), "source": source, "kind": kind}
            assert self._proc.stdin is not None and self._proc.stdout is not None
            try:
                self._proc.stdin.write(json.dumps(req) + "\n")
                self._proc.stdin.flush()
                line = self._proc.stdout.readline()
            except (BrokenPipeError, OSError) as exc:
                self._proc = None
                raise AstError(f"apex parser server died: {exc}") from exc
            if not line:
                self._proc = None
                raise AstError("apex parser server closed its output")
            resp = json.loads(line)
        if resp.get("error"):
            raise AstError(str(resp["error"]))
        tree = resp.get("tree")
        if not isinstance(tree, list):
            raise AstError("apex parser returned no tree")
        return AstResult(tree=tree, errors=list(resp.get("errors") or []))

    def close(self) -> None:
        with self._lock:
            proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            if proc.stdin is not None:
                proc.stdin.close()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def version(self) -> dict[str, str]:
        out = subprocess.run(
            [self.node, str(self.directory / "apex_ast.js"), "--version"],
            cwd=str(self.directory),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        data = json.loads(out)
        return {str(k): str(v) for k, v in data.items()}


_server: ApexAstServer | None = None
_server_lock = threading.Lock()


def get_server() -> ApexAstServer:
    """Process-wide server, created lazily."""
    global _server
    with _server_lock:
        if _server is None:
            if not is_available():
                raise AstUnavailable(f"apex parser not installed under {parser_dir()}")
            _server = ApexAstServer()
        return _server


def parse(source: str, kind: str = "auto") -> AstResult:
    return get_server().parse(source, kind)
