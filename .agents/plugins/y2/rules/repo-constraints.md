# Rule: Repository Constraints

1. **Python Dependencies & Compatibility:** Runtime dependencies are limited strictly to Python standard library and PyYAML. Code must maintain compatibility with Python 3.9 (no `match` statements, no `X | Y` type unions outside annotations, no `zip(strict=True)`, no `dataclass(slots=True)` or `kw_only`). Use `from __future__ import annotations` across all Python source files.
2. **Bash Host Compatibility:** Host execution logic (`y2shared/hostenv.py`, wrappers) must execute correctly under macOS default bash 3.2 (no associative arrays `declare -A`, no `${var,,}` parameter expansion, no `readarray`/`mapfile`).
3. **PowerShell Windows Payloads:** Windows scripts targeting Windows Server 2016/2022 must run cleanly under Windows PowerShell 5.1 (UTF-8 BOM encoding, avoiding PowerShell Core 7+ features).
4. **Golden File Preservation:** Golden compiled scripts in `tests/golden/hip_compliance_remote.sh` and `tests/golden/windows_baseline_remote.ps1` must remain byte-identical unless a task card explicitly authorizes updating them.

