"""Collect list management (<parts>/collect_list.jsonl)."""

from __future__ import annotations

import json
import os
import threading
import warnings
from pathlib import Path
from typing import Any, Mapping

from y2b.inputcsv import VMRow


class CollectList:
    """Manages appending and reading the collect list (<parts>/collect_list.jsonl)."""

    def __init__(self, parts_dir: Path | str) -> None:
        self.parts_dir = Path(parts_dir)
        self.path = self.parts_dir / "collect_list.jsonl"
        self._lock = threading.Lock()

    def append(self, record: dict[str, Any]) -> None:
        """
        Append a single record atomically.
        Thread-safe: takes threading.Lock, writes one JSON line, flushes and fsyncs.
        """
        self.parts_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record) + "\n"
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())

    def load(
        self,
        rows: list[VMRow] | Mapping[int, VMRow] | None = None,
    ) -> dict[int, dict[str, Any]]:
        """
        Load records from collect_list.jsonl.
        Returns a dict mapping index (int) to the latest record for that index.
        Last record wins if an index appears multiple times.
        A malformed last line (crash mid-write) is ignored with a warning.
        A malformed line anywhere else raises ValueError.
        If rows is provided, verifies that record['vm_name'] matches rows[index].vm_name;
        a mismatch raises ValueError.
        """
        if not self.path.exists():
            return {}

        with open(self.path, "r", encoding="utf-8") as f:
            content = f.read()

        lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
        if not lines:
            return {}

        records_by_index: dict[int, dict[str, Any]] = {}
        rows_by_idx: Mapping[int, VMRow] = {}
        if rows is not None:
            if isinstance(rows, Mapping):
                rows_by_idx = rows
            else:
                rows_by_idx = {r.index: r for r in rows}

        for i, line in enumerate(lines):
            is_last = (i == len(lines) - 1)
            try:
                data = json.loads(line)
                if not isinstance(data, dict) or "index" not in data or "vm_name" not in data:
                    raise ValueError(f"Record must be a JSON object with 'index' and 'vm_name': {line}")
            except Exception as e:
                if is_last:
                    warnings.warn(f"Ignoring malformed last line in {self.path}: {e}")
                    continue
                raise ValueError(f"Malformed line {i + 1} in {self.path}: {e}") from e

            idx = int(data["index"])
            vm_name = str(data["vm_name"])

            if rows_by_idx:
                expected_row = rows_by_idx.get(idx)
                if expected_row is None:
                    raise ValueError(f"Collect list row index {idx} not found in input CSV rows")
                if expected_row.vm_name != vm_name:
                    raise ValueError(
                        f"Input CSV changed: collect list has VM '{vm_name}' at index {idx}, "
                        f"but input CSV has '{expected_row.vm_name}'"
                    )

            if data.get("failed"):
                records_by_index.pop(idx, None)
            else:
                records_by_index[idx] = data

        return records_by_index
