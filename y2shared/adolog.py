"""Azure DevOps (ADO) logging integration and group formatting under TF_BUILD (AC-15)."""

from __future__ import annotations

import os
import threading
from typing import Callable, Sequence

from y2shared.inputcsv import VMRow


def is_tf_build() -> bool:
    """Return True if running in Azure DevOps pipeline (TF_BUILD case-insensitively == 'true')."""
    return os.environ.get("TF_BUILD", "").strip().lower() == "true"


def neutralise(line: str) -> str:
    """Neutralise ADO command prefixes so VM text cannot inject ADO commands."""
    return line.replace("##vso[", "#vso[").replace("##[", "#[")


def emit_warning(msg: str) -> None:
    """Print single-line ADO warning issue if running under TF_BUILD."""
    if is_tf_build():
        print(f"##vso[task.logissue type=warning]{msg}")


class ProcessLogSink:
    """Collects per-VM lines produced during the current process for TF_BUILD groups."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lines: dict[int, list[str]] = {}

    def add(self, index: int, vm_name: str, line: str) -> None:
        with self._lock:
            self._lines.setdefault(index, []).append(line)

    def drain(self, index: int) -> list[str]:
        with self._lock:
            return self._lines.pop(index, [])


def print_vm_groups(
    rows: Sequence[VMRow],
    sink: ProcessLogSink,
    outcome_fn: Callable[[VMRow], str],
) -> None:
    """
    Print one ADO group per VM this process worked on, in index order:
    ##[group]<vm_name> · <outcome>
    <neutralised lines>
    ##[endgroup]
    """
    for row in sorted(rows, key=lambda r: r.index):
        outcome = outcome_fn(row)
        lines = sink.drain(row.index)
        print(f"##[group]{row.vm_name} · {outcome}")
        for ln in lines:
            print(neutralise(ln))
        print("##[endgroup]")

