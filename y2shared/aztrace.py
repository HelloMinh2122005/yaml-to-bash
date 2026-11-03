"""Core az tracing, secret redaction, and trace recording (AC-11, AC-12, AC-14)."""

from __future__ import annotations

import atexit
import os
import re
import shlex
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Union

_SECRET_FLAG_RE = re.compile(
    r"^--[a-zA-Z0-9_-]*(?:password|secret|token|key)[a-zA-Z0-9_-]*$",
    re.IGNORECASE,
)

_SECRET_TEXT_RE = re.compile(
    r"(--[A-Za-z0-9_-]*(?:password|secret|token|key)[A-Za-z0-9_-]*)(=|\s+)((?:\"[^\"]*\")|(?:'[^']*')|(?:[^\s\"'-]\S*))",
    re.IGNORECASE,
)


def redact_argv(argv: Sequence[str]) -> List[str]:
    """Redact secrets in argv flags (case-insensitive for password/secret/token/key)."""
    out: List[str] = []
    skip_next = False
    for i, token in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if token.startswith("--") and "=" in token:
            k, v = token.split("=", 1)
            if _SECRET_FLAG_RE.match(k):
                out.append(f"{k}=***")
                continue
        elif token.startswith("--") and _SECRET_FLAG_RE.match(token):
            out.append(token)
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                out.append("***")
                skip_next = True
            continue
        out.append(token)
    return out


def redact_text(text: str) -> str:
    """Redact secret flag values in text (AC-14)."""
    return _SECRET_TEXT_RE.sub(r"\1\2***", text)


def quote_argv(argv: Sequence[str]) -> str:
    """Return shlex-quoted representation of redacted argv."""
    return shlex.join(redact_argv(argv))


def format_engine_line(
    kind: str,
    attempt: int,
    rc: Union[int, str],
    status: str,
    dur: float,
    argv: Sequence[str],
) -> str:
    """Format an engine trace line per AC-11."""
    quoted = quote_argv(argv)
    return f"engine:{kind} attempt={attempt} rc={rc} status={status} dur={dur:.1f}s :: {quoted}"


def format_script_line(
    section: str,
    name: str,
    rc: int,
    argv: Sequence[str],
) -> str:
    """Format a host-script trace line per AC-12."""
    quoted = quote_argv(argv)
    return f"script:{section}:{name} rc={rc} :: {quoted}"


@dataclass
class EngineEntry:
    kind: str
    attempt: int
    rc: Union[int, str]
    status: str
    dur: float
    argv: List[str]


@dataclass
class ScriptCall:
    section: str
    name: str
    rc: int
    argv: List[str]


class TraceRecorder:
    """Process-global trace recorder tracking executed commands per VM index."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: Dict[int, List[Union[EngineEntry, ScriptCall]]] = {}
        self._expected: Dict[int, Set[str]] = {}

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()
            self._expected.clear()

    def record_engine(
        self,
        index: int,
        kind: str,
        attempt: int,
        rc: Union[int, str],
        status: str,
        dur: float,
        argv: Sequence[str],
    ) -> None:
        with self._lock:
            entry = EngineEntry(
                kind=kind,
                attempt=attempt,
                rc=rc,
                status=status,
                dur=dur,
                argv=list(argv),
            )
            self._entries.setdefault(index, []).append(entry)

    def record_script(
        self,
        index: int,
        section: str,
        name: str,
        rc: int,
        argv: Sequence[str],
    ) -> None:
        with self._lock:
            entry = ScriptCall(
                section=section,
                name=name,
                rc=rc,
                argv=list(argv),
            )
            self._entries.setdefault(index, []).append(entry)

    def entries(self, index: int) -> List[Union[EngineEntry, ScriptCall]]:
        with self._lock:
            return list(self._entries.get(index, []))

    def expect(self, index: int, kind: str) -> None:
        with self._lock:
            self._expected.setdefault(index, set()).add(kind)

    def expected(self, index: int) -> Set[str]:
        with self._lock:
            return set(self._expected.get(index, set()))


RECORDER = TraceRecorder()

_SHIM_DIR: Optional[Path] = None
_SHIM_LOCK = threading.Lock()


def ensure_shim_dir() -> Path:
    """Ensure per-process temp directory with executable az wrapper exists."""
    global _SHIM_DIR
    with _SHIM_LOCK:
        if _SHIM_DIR is not None and _SHIM_DIR.exists():
            return _SHIM_DIR

        tmp = Path(tempfile.mkdtemp(prefix="y2_az_shim_"))
        atexit.register(lambda: shutil.rmtree(str(tmp), ignore_errors=True))

        src_shim = Path(__file__).resolve().parent / "azshim" / "az"
        dst_shim = tmp / "az"
        if src_shim.exists():
            shutil.copy2(src_shim, dst_shim)
            dst_shim.chmod(0o755)

        _SHIM_DIR = tmp
        return tmp


class ScriptTrace:
    """Context holding temporary trace file and environment for host-script az tracing."""

    def __init__(self, section: str, name: str) -> None:
        self.section = section
        self.name = name
        shim_dir = ensure_shim_dir()

        fd, path = tempfile.mkstemp(prefix="y2_az_trace_", suffix=".tmp")
        os.close(fd)
        # Ensure mode 0600
        os.chmod(path, 0o600)
        self.trace_file = Path(path)

        cur_path = os.environ.get("PATH", "")
        self.env = {
            "PATH": f"{shim_dir}:{cur_path}",
            "Y2_AZ_SHIM_DIR": str(shim_dir),
            "Y2_AZ_TRACE_FILE": str(self.trace_file),
            "Y2_AZ_TRACE_CTX": f"{section}:{name}",
        }

    def collect(self) -> List[ScriptCall]:
        """Read NUL-delimited records from trace file and clean up."""
        calls: List[ScriptCall] = []
        try:
            if self.trace_file.exists():
                data = self.trace_file.read_bytes()
                if data:
                    raw_text = data.decode("utf-8", errors="replace")
                    tokens = raw_text.split("\x00")
                    if tokens and tokens[-1] == "":
                        tokens.pop()
                    idx = 0
                    while idx + 2 < len(tokens):
                        ctx = tokens[idx]
                        try:
                            rc = int(tokens[idx + 1])
                            argc = int(tokens[idx + 2])
                        except ValueError:
                            break
                        idx += 3
                        argv = tokens[idx : idx + argc]
                        idx += argc
                        section, _, name = ctx.partition(":")
                        calls.append(ScriptCall(section=section, name=name, rc=rc, argv=redact_argv(argv)))
        finally:
            if self.trace_file.exists():
                try:
                    self.trace_file.unlink()
                except OSError:
                    pass
        return calls


def prepare_script_trace(section: str, name: str) -> ScriptTrace:
    return ScriptTrace(section=section, name=name)
