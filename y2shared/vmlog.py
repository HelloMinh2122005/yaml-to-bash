"""Per-VM log recorder."""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Callable, Optional, Sequence

from y2shared.asciiguard import fold_ascii, record, vm_report
from y2shared.aztrace import format_engine_line, format_script_line

_LINE_SINK: Optional[Callable[[int, str, str], None]] = None


def set_line_sink(fn: Optional[Callable[[int, str, str], None]]) -> None:
    """Set global line sink for TF_BUILD log grouping."""
    global _LINE_SINK
    _LINE_SINK = fn


def tee_lines(index: int, vm_name: str, lines: list[str]) -> None:
    """Send lines to the line sink if one is installed."""
    cur_hook = "unknown"
    for ln in lines:
        m = re.search(r"\[post_run:([^\]]+)\]", ln)
        if m:
            cur_hook = m.group(1)
        folded_ln, n = fold_ascii(ln)
        if n > 0:
            if m:
                record(vm_name, f"post_run:{m.group(1)}", n)
            elif "[az]" in ln or "az " in ln.lower():
                record(vm_name, "az", n)
            else:
                record(vm_name, f"post_run:{cur_hook}", n)
        if _LINE_SINK is not None:
            _LINE_SINK(index, vm_name, folded_ln)


class VMLogger:
    """Collects and formats log lines for an individual VM run."""

    def __init__(self, vm_name: str, index: int, sink: bool = True):
        self.vm_name = vm_name
        self.index = index
        self.lines: list[str] = []
        self._sink = sink
        self._add_header()

    @classmethod
    def from_lines(cls, vm_name: str, index: int, lines: list[str], sink: bool = True) -> VMLogger:
        """Create a VMLogger instance populated from previous lines (e.g. from phase 1)."""
        logger = cls.__new__(cls)
        logger.vm_name = vm_name
        logger.index = index
        logger.lines = list(lines)
        logger._sink = sink
        return logger

    def _now_str(self) -> str:
        return datetime.datetime.now().astimezone().isoformat()

    def _append(self, line: str) -> None:
        folded, _ = fold_ascii(line)
        self.lines.append(folded)
        if self._sink and _LINE_SINK is not None:
            _LINE_SINK(self.index, self.vm_name, folded)

    def _add_header(self) -> None:
        self._append(f"==================================================")
        self._append(f"VM: {self.vm_name} (Row index {self.index}) - Started {self._now_str()}")
        self._append(f"==================================================")

    def log(self, message: str) -> None:
        """Add a general log entry with timestamp."""
        self._append(f"[{self._now_str()}] {message}")

    def log_precheck(self, name: str, stdout: str, action: str, note: str) -> None:
        """Log precheck result line matching specification format."""
        folded_stdout, n = fold_ascii(stdout)
        if n > 0:
            record(self.vm_name, f"precheck:{name}", n)
        folded_action, _ = fold_ascii(action)
        folded_note, _ = fold_ascii(note)
        self._append(f"[{self._now_str()}] [precheck {name}] -> \"{folded_stdout}\" -> {folded_action}: {folded_note}")

    def log_dispatch(self, message: str) -> None:
        self._append(f"[{self._now_str()}] [dispatch] {message}")

    def log_collect(self, col: str, val: str) -> None:
        folded_val, n = fold_ascii(val)
        if n > 0:
            record(self.vm_name, col, n)
        self._append(f"[{self._now_str()}] [collect] {col} = {folded_val}")

    def log_error(self, message: str) -> None:
        folded_msg, n = fold_ascii(message)
        if n > 0 and ("az" in message.lower() or "[az]" in message):
            record(self.vm_name, "az", n)
        self._append(f"[{self._now_str()}] [ERROR] {folded_msg}")

    def log_az_engine(
        self,
        kind: str,
        attempt: int,
        rc: int | str,
        status: str,
        dur: float,
        argv: Sequence[str],
    ) -> None:
        line = format_engine_line(kind, attempt, rc, status, dur, argv)
        folded_line, n = fold_ascii(line)
        if n > 0:
            record(self.vm_name, "az", n)
        self._append(f"[{self._now_str()}] [az] {folded_line}")

    def log_az_script(
        self,
        section: str,
        name: str,
        rc: int,
        argv: Sequence[str],
    ) -> None:
        line = format_script_line(section, name, rc, argv)
        folded_line, n = fold_ascii(line)
        if n > 0:
            record(self.vm_name, "az", n)
        self._append(f"[{self._now_str()}] [az] {folded_line}")

    def log_az_note(self, note: str) -> None:
        folded_note, n = fold_ascii(note)
        if n > 0:
            record(self.vm_name, "az", n)
        self._append(f"[{self._now_str()}] [az] {folded_note}")

    def finalize(self, outcome: str) -> None:
        folded_outcome, n = fold_ascii(outcome)
        if n > 0 and outcome.startswith("ERROR:az:"):
            record(self.vm_name, "az", n)
        for rpt_line in vm_report(self.vm_name):
            if rpt_line not in self.lines:
                self._append(rpt_line)
        self._append(f"[{self._now_str()}] Final outcome: {folded_outcome}")
        self._append(f"==================================================\n")

    def get_text(self) -> str:
        return "\n".join(self.lines) + "\n"

    def write_to_file(self, file_path: str | Path) -> None:
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.get_text())

