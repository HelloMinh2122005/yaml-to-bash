"""Per-VM log recorder."""

from __future__ import annotations

import datetime
from pathlib import Path


class VMLogger:
    """Collects and formats log lines for an individual VM run."""

    def __init__(self, vm_name: str, index: int):
        self.vm_name = vm_name
        self.index = index
        self.lines: list[str] = []
        self._add_header()

    @classmethod
    def from_lines(cls, vm_name: str, index: int, lines: list[str]) -> VMLogger:
        """Create a VMLogger instance populated from previous lines (e.g. from phase 1)."""
        logger = cls.__new__(cls)
        logger.vm_name = vm_name
        logger.index = index
        logger.lines = list(lines)
        return logger

    def _now_str(self) -> str:
        return datetime.datetime.now().astimezone().isoformat()

    def _add_header(self) -> None:
        self.lines.append(f"==================================================")
        self.lines.append(f"VM: {self.vm_name} (Row index {self.index}) — Started {self._now_str()}")
        self.lines.append(f"==================================================")

    def log(self, message: str) -> None:
        """Add a general log entry with timestamp."""
        self.lines.append(f"[{self._now_str()}] {message}")

    def log_precheck(self, name: str, stdout: str, action: str, note: str) -> None:
        """Log precheck result line matching specification format."""
        self.lines.append(f"[{self._now_str()}] [precheck {name}] → \"{stdout}\" → {action}: {note}")

    def log_dispatch(self, message: str) -> None:
        self.lines.append(f"[{self._now_str()}] [dispatch] {message}")

    def log_collect(self, col: str, val: str) -> None:
        self.lines.append(f"[{self._now_str()}] [collect] {col} = {val}")

    def log_error(self, message: str) -> None:
        self.lines.append(f"[{self._now_str()}] [ERROR] {message}")

    def finalize(self, outcome: str) -> None:
        self.lines.append(f"[{self._now_str()}] Final outcome: {outcome}")
        self.lines.append(f"==================================================\n")

    def get_text(self) -> str:
        return "\n".join(self.lines) + "\n"

    def write_to_file(self, file_path: str | Path) -> None:
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.get_text())

