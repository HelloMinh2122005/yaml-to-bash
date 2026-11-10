"""ASCII guard module for folding runtime values and tracking replacements."""

from __future__ import annotations

import importlib
from pathlib import Path
import threading
from typing import Any

unicodedata = importlib.import_module("unicodedata")


def fold_ascii(text: Any) -> tuple[str, int]:
    """Fold text to pure ASCII, replacing non-ASCII characters.

    Coerces non-str to str; fast path text.isascii() -> (text, 0).
    Map 'đ' -> 'd', 'Đ' -> 'D'.
    NFKD decompose, remove unicodedata.combining marks.
    Replace remaining non-ASCII characters with '?'.
    Return (folded_str, replaced_count) where replaced_count is number of altered
    input non-ASCII code points. Never raises.
    """
    try:
        if not isinstance(text, str):
            try:
                text = str(text)
            except Exception:
                text = ""
        if text.isascii():
            return text, 0
        replaced_count = sum(1 for c in text if ord(c) > 127)
        s = text.replace("đ", "d").replace("Đ", "D")
        decomposed = unicodedata.normalize("NFKD", s)
        folded = "".join(
            c if ord(c) < 128 else "?"
            for c in decomposed
            if unicodedata.combining(c) == 0
        )
        return folded, replaced_count
    except Exception:
        try:
            s_str = str(text) if text is not None else ""
            replaced_count = sum(1 for c in s_str if ord(c) > 127)
            folded = "".join(c if ord(c) < 128 else "?" for c in s_str)
            return folded, replaced_count
        except Exception:
            return "", 0


class _AsciiGuard:
    """Thread-safe counter tracking sanitized non-ASCII values per VM and source."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str], int] = {}

    def record(self, vm: str, source: str, n: int) -> None:
        if n <= 0:
            return
        with self._lock:
            if (vm, source) not in self._records:
                self._records[(vm, source)] = n
            else:
                self._records[(vm, source)] = max(self._records[(vm, source)], n)

    def vm_report(self, vm: str) -> list[str]:
        with self._lock:
            lines: list[str] = []
            for (v, source), count in sorted(self._records.items(), key=lambda item: item[0][1]):
                if v == vm:
                    lines.append(f"[ascii] {source}: {count} non-ASCII char(s) replaced")
            return lines

    def run_totals(self) -> tuple[int, int]:
        with self._lock:
            total_values = len(self._records)
            vms = {v for v, _ in self._records.keys()}
            return total_values, len(vms)

    def reset(self) -> None:
        with self._lock:
            self._records.clear()


_GUARD = _AsciiGuard()


def record(vm: str, source: str, n: int) -> None:
    """Record sanitization of n non-ASCII characters for (vm, source)."""
    _GUARD.record(vm, source, n)


def vm_report(vm: str) -> list[str]:
    """Return formatted report lines for a VM in deterministic order."""
    return _GUARD.vm_report(vm)


def run_totals() -> tuple[int, int]:
    """Return (total_values_sanitized, total_vms_count)."""
    return _GUARD.run_totals()


def reset() -> None:
    """Reset all counters."""
    _GUARD.reset()


def scan_non_ascii(path: str | Path) -> list[str]:
    """Scan a file for non-ASCII characters or invalid UTF-8.

    Decodes bytes as UTF-8.
    If invalid UTF-8 bytes: reports '<path>: invalid UTF-8 at byte <N>' without crashing.
    If UTF-8 BOM: reports '<path>:1:1: non-ASCII character U+FEFF'.
    For each non-ASCII character (ord > 127), records:
    '<path>:<line>:<col>: non-ASCII character U+<codepoint:04X>' (1-based line & col).
    Caps at 20 offending lines per file, followed by:
    '<path>: <N> more non-ASCII characters'.
    """
    path_str = str(path)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return [f"{path_str}: failed to read file: {e}"]

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        return [f"{path_str}: invalid UTF-8 at byte {exc.start}"]

    total_non_ascii = sum(1 for c in text if ord(c) > 127)
    if total_non_ascii == 0:
        return []

    lines = text.splitlines(keepends=True)
    errors: list[str] = []
    offending_lines_count = 0

    for line_idx, line in enumerate(lines, start=1):
        has_non_ascii = any(ord(c) > 127 for c in line)
        if not has_non_ascii:
            continue

        if offending_lines_count < 20:
            for col_idx, ch in enumerate(line, start=1):
                if ord(ch) > 127:
                    errors.append(
                        f"{path_str}:{line_idx}:{col_idx}: non-ASCII character U+{ord(ch):04X}"
                    )
                    break
            offending_lines_count += 1

    remaining = total_non_ascii - offending_lines_count
    if remaining > 0:
        errors.append(f"{path_str}: {remaining} more non-ASCII characters")

    return errors
