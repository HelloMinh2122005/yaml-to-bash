# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This repository provides two declarative Azure VM automation CLI tools backed by a shared orchestration engine:
- `y2b` (`yaml_to_bash`): Automates Azure Linux VMs (`RunShellScript` with bash payloads).
- `y2p` (`yaml_to_powershell`): Automates Azure Windows VMs (`RunPowerShellScript` with Windows PowerShell 5.1 payloads).

Target scale is 500+ VMs per run. Each target VM gets exactly one runCommand POST in Phase 1 (`az rest`, not `run-command invoke --no-wait`, which silently drops the request before azure-cli 2.85.0), and results are collected via an event scheduler in Phase 2. All results are merged into `result.csv` and `result.log` in original input CSV order.

User-facing docs: [README.md](README.md) and [USER_GUIDE.md](USER_GUIDE.md).

## Commands

```bash
# Environment setup
source setup_venv.sh                       # create .venv, install PyYAML + pytest, put bin/y2b and bin/y2p on PATH

# Run test suite
python3 -m pytest tests/ -v                # full test suite (100+ tests); run from repo root
python3 -m pytest tests/test_y2p_unit.py -v # single test file

# Linux CLI (y2b)
y2b validate examples/hip-compliance/job.yaml
y2b compile  examples/hip-compliance/job.yaml [-o remote.sh]
y2b run      examples/hip-compliance/job.yaml --dry-run
y2b run      examples/hip-compliance/job.yaml --resume

# Windows CLI (y2p)
y2p validate examples/windows-baseline/job.yaml
y2p compile  examples/windows-baseline/job.yaml [-o remote.ps1]
y2p run      examples/windows-baseline/job.yaml --dry-run
y2p run      examples/windows-baseline/job.yaml --resume
```

`bin/y2b` and `bin/y2p` are cwd-independent wrapper scripts setting `PYTHONPATH` and launching `python3 -m y2b` / `python3 -m y2p`. Runtime dependencies are strictly Python stdlib and PyYAML. Maintain Python 3.9 compatibility across the codebase (no `match`, no `|` union outside annotations, no `zip(strict=)`, no `dataclass(slots=/kw_only=)`).

## Repository Layout

```
yaml_to_bash/
├── bin/
│   ├── y2b                          # Cwd-independent wrapper for y2b
│   └── y2p                          # Cwd-independent wrapper for y2p
├── y2shared/                        # Shared OS-agnostic engine
│   ├── cli.py                       # CLI parsing and commands (validate, compile, run)
│   ├── profile.py                   # Profile seam separating y2b and y2p
│   ├── config.py                    # Job dataclasses and YAML loading
│   ├── inputcsv.py                  # CSV parsing and OS validation
│   ├── hostenv.py                   # macOS host bash, ulimit, and caffeinate management
│   ├── precheck.py                  # Host-side bash prechecks and subscription caching
│   ├── dispatcher.py                # Azure CLI invoke, backoff retries, and auth breaker
│   ├── collector.py                 # Variable resolution and CSV row assembly
│   ├── collectlist.py               # Phase 1/Phase 2 JSONL state ledger
│   ├── runner.py                    # Two-phase concurrent runner orchestrator
│   ├── merger.py                    # Deterministic part-file merger
│   ├── parts.py                     # Per-VM part directory and run.json manager
│   ├── shell.py                     # Safe host-side bash execution
│   ├── vmlog.py                     # In-memory and per-VM buffered logging
│   ├── validate.py                  # Shared job validation logic
│   └── scan.py                      # Generic step variable scanner
├── y2b/                             # Linux frontend
│   ├── profile.py                   # Linux Profile implementation
│   ├── compiler.py                  # Bash script renderer and bash -n syntax validator
│   ├── validate.py                  # Linux-specific validation (res_file path rules)
│   └── templates/                   # remote.sh.tpl, collect.sh.tpl
├── y2p/                             # Windows frontend
│   ├── profile.py                   # Windows Profile implementation
│   ├── compiler.py                  # PowerShell script renderer
│   ├── validate.py                  # Windows-specific validation (res_file drive paths, YAML quoting)
│   └── templates/                   # remote.ps1.tpl, collect.ps1.tpl
├── examples/
│   ├── hip-compliance/              # Linux compliance job example
│   └── windows-baseline/            # Windows baseline job example
└── tests/
    ├── fake_az/az                   # Stand-in Azure CLI mock
    ├── golden/
    │   ├── hip_compliance_remote.sh     # Golden compiled script for y2b
    │   └── windows_baseline_remote.ps1  # Golden compiled script for y2p
    ├── test_compiler.py             # y2b compiler & Rocky Linux 9 container tests
    ├── test_y2p_container.py        # y2p PowerShell container tests
    ├── test_y2p_unit.py             # y2p unit tests
    ├── test_y2p_integration.py      # y2p 20-VM mock Azure integration tests
    └── ...                          # Engine, precheck, scheduler, and dispatcher tests
```

## Architecture & Seams

- **`Profile` (`y2shared.profile.Profile`)**: The abstraction seam separating OS specifics. Defines tool name, target OS family, Azure command ID (`RunShellScript` vs `RunPowerShellScript`), file templates, compiler function, validation rules, and parser function.
- **Compiler**:
  - `y2b`: Builds composite bash script with `write_result KEY VALUE` functions and step isolation.
  - `y2p`: Builds composite PowerShell script with `Invoke-Y2Step` table, `write_result` (base64 into `$ResFile`), process timeout killing (`taskkill /T /F`), and Windows two-element JSON parsing.
- **Host Prechecks & Computed Columns**: Always run on the **host runner using Bash** (via `y2shared.hostenv.resolve_bash()`) for both `y2b` and `y2p`.
- **Golden Files**:
  - `tests/golden/hip_compliance_remote.sh`: Must match `y2b compile examples/hip-compliance/job.yaml` byte-for-byte.
  - `tests/golden/windows_baseline_remote.ps1`: Must match `y2p compile examples/windows-baseline/job.yaml` byte-for-byte.
  - Recompile using `-o <path>` whenever templates or example jobs are updated.
- **Testing**:
  - `tests/fake_az/az` simulates Azure CLI responses, throttling, conflict busy states, and auth errors.
  - Container tests execute actual payloads using `rockylinux:9` (bash) and `mcr.microsoft.com/powershell:7.4-ubuntu-22.04` (pwsh).
