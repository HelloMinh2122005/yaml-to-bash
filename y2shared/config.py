"""Configuration models and YAML loader for yaml_to_bash."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import yaml


DURATION_REGEX = re.compile(r"^(\d+(?:\.\d+)?(?:h|m|s))+$")
PART_REGEX = re.compile(r"(\d+(?:\.\d+)?)([hms])")


def parse_duration(val: Any) -> float:
    """
    Parse a duration value into seconds (float).
    Accepts non-negative numbers (int, float) or duration strings like:
    90, '90', '30s', '5m', '1h', '1m30s'.
    Raises ValueError on invalid formats, empty strings, or negative values.
    """
    if isinstance(val, bool):
        raise ValueError(f"Boolean value '{val}' is not a valid duration")
    if isinstance(val, (int, float)):
        if val < 0:
            raise ValueError(f"Duration cannot be negative, got {val}")
        return float(val)
    if not isinstance(val, str):
        raise ValueError(f"Invalid duration type: {type(val).__name__}")

    s = val.strip()
    if not s:
        raise ValueError("Duration string cannot be empty")

    try:
        num = float(s)
        if num < 0:
            raise ValueError(f"Duration cannot be negative, got '{s}'")
        return num
    except ValueError:
        pass

    if not DURATION_REGEX.match(s):
        raise ValueError(f"Invalid duration format: '{val}'")

    total_seconds = 0.0
    for amount_str, unit in PART_REGEX.findall(s):
        amount = float(amount_str)
        if unit == "h":
            total_seconds += amount * 3600
        elif unit == "m":
            total_seconds += amount * 60
        elif unit == "s":
            total_seconds += amount

    return total_seconds


@dataclass
class Precheck:
    name: str
    script: str | None = None
    script_path: Path | None = None
    result: dict[str, dict[str, str]] = field(default_factory=dict)
    timeout: int = 120
    cache: str | None = None
    phases: list[str] = field(default_factory=lambda: ["invoke"])


@dataclass
class PostRun:
    name: str
    script: str | None = None
    script_path: Path | None = None
    timeout: int = 120
    result: dict[str, dict[str, str]] = field(default_factory=dict)


@dataclass
class Step:
    name: str
    script: str | None = None
    script_path: Path | None = None
    timeout: int = 120


@dataclass
class RunCommand:
    res_file: str
    log_file: str
    steps: list[Step] = field(default_factory=list)


@dataclass
class Invoke:
    precheck: list[Precheck] = field(default_factory=list)
    runcommand: RunCommand | None = None


@dataclass
class Column:
    column: str
    source: str | None = None
    var: str | None = None
    script: str | None = None
    script_path: Path | None = None
    timeout: int = 30


@dataclass
class Job:
    yaml_path: Path
    yaml_dir: Path
    raw_yaml: dict[str, Any]
    job_hash: str
    input: Path
    output_log: Path
    output_csv: Path
    actions: str = "skip"
    parallel: int = 10
    sleep_before_collect: float = 0.0
    collect_retry_interval: float = 30.0
    collect_retries: int = 3
    run_timeout: int = 1800
    invoke: Invoke = field(default_factory=Invoke)
    collect: list[Column] = field(default_factory=list)
    pre_run: list[Precheck] = field(default_factory=list)
    post_run: list[PostRun] = field(default_factory=list)

    @property
    def starts_vms(self) -> bool:
        return self.actions in ("start", "start-stop")

    @property
    def stops_after(self) -> bool:
        return self.actions in ("stop", "start-stop")

    def __post_init__(self) -> None:
        if not self.pre_run and self.invoke and self.invoke.precheck:
            self.pre_run = self.invoke.precheck
        elif self.pre_run and self.invoke and not self.invoke.precheck:
            self.invoke.precheck = self.pre_run


def compute_content_hash(text: str) -> str:
    """Calculate sha256 hash of a string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_yaml_raw(yaml_path: str | Path) -> tuple[dict[str, Any], str]:
    """Read and parse YAML file, returning the raw dict and the file hash."""
    path = Path(yaml_path).resolve()
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    raw = yaml.safe_load(content)
    if not isinstance(raw, dict):
        raise ValueError(f"YAML at {path} must define a mapping/dictionary at the root")
    file_hash = compute_content_hash(content)
    return raw, file_hash


def parse_job_from_dict(raw: dict[str, Any], yaml_path: str | Path, file_hash: str | None = None) -> Job:
    """Instantiate a Job dataclass from a raw dict with paths resolved relative to yaml_path."""
    path = Path(yaml_path).resolve()
    yaml_dir = path.parent
    if file_hash is None:
        file_hash = compute_content_hash(yaml.dump(raw, sort_keys=True))

    def resolve_path(p: str | None) -> Path | None:
        if p is None:
            return None
        cand = Path(p)
        if cand.is_absolute():
            return cand
        return (yaml_dir / cand).resolve()

    # Prechecks (root pre_run or legacy invoke.precheck)
    invoke_raw = raw.get("invoke") or {}
    has_pre_run = "pre_run" in raw
    has_invoke_precheck = "precheck" in invoke_raw
    if has_pre_run and has_invoke_precheck:
        raise ValueError("Cannot define both root 'pre_run' and 'invoke.precheck'")

    if has_pre_run:
        prechecks_raw = raw.get("pre_run") or []
    else:
        prechecks_raw = invoke_raw.get("precheck") or []

    prechecks: list[Precheck] = []
    for item in prechecks_raw:
        if not isinstance(item, dict):
            continue
        res_raw = item.get("result") or {}
        # Normalize result keys to strings
        res_normalized: dict[str, dict[str, str]] = {}
        if isinstance(res_raw, dict):
            for k, v in res_raw.items():
                if isinstance(v, dict):
                    res_normalized[str(k)] = {str(vk): str(vv) for vk, vv in v.items()}
                else:
                    res_normalized[str(k)] = {}

        p_script_path = resolve_path(item.get("script_path"))
        phase_val = item.get("phase") if "phase" in item else item.get("phases")
        if phase_val is None:
            phases = ["invoke"]
        elif isinstance(phase_val, str):
            phases = [phase_val]
        elif isinstance(phase_val, list):
            phases = [str(p) for p in phase_val]
        else:
            phases = ["invoke"]

        prechecks.append(
            Precheck(
                name=str(item.get("name", "")),
                script=item.get("script"),
                script_path=p_script_path,
                result=res_normalized,
                timeout=int(item.get("timeout", 120)),
                cache=item.get("cache"),
                phases=phases,
            )
        )

    # Post-run hooks
    post_run_raw = raw.get("post_run") or []
    post_runs: list[PostRun] = []
    for item in post_run_raw:
        if not isinstance(item, dict):
            continue
        res_raw = item.get("result") or {}
        res_normalized: dict[str, dict[str, str]] = {}
        if isinstance(res_raw, dict):
            for k, v in res_raw.items():
                if isinstance(v, dict):
                    res_normalized[str(k)] = {str(vk): str(vv) for vk, vv in v.items()}
                else:
                    res_normalized[str(k)] = {}

        pr_script_path = resolve_path(item.get("script_path"))
        post_runs.append(
            PostRun(
                name=str(item.get("name", "")),
                script=item.get("script"),
                script_path=pr_script_path,
                timeout=int(item.get("timeout", 120)),
                result=res_normalized,
            )
        )

    # Runcommand
    rc_raw = invoke_raw.get("runcommand") or {}
    steps_raw = rc_raw.get("steps") or []
    steps: list[Step] = []
    for s in steps_raw:
        if not isinstance(s, dict):
            continue
        s_script_path = resolve_path(s.get("script_path"))
        steps.append(
            Step(
                name=str(s.get("name", "")),
                script=s.get("script"),
                script_path=s_script_path,
                timeout=int(s.get("timeout", 120)),
            )
        )

    runcommand = RunCommand(
        res_file=str(rc_raw.get("res_file", "")),
        log_file=str(rc_raw.get("log_file", "")),
        steps=steps,
    )

    invoke = Invoke(precheck=prechecks, runcommand=runcommand)

    # Collect
    collect_raw = raw.get("collect") or []
    columns: list[Column] = []
    for c in collect_raw:
        if not isinstance(c, dict):
            continue
        c_script_path = resolve_path(c.get("script_path"))
        columns.append(
            Column(
                column=str(c.get("column", "")),
                source=c.get("source"),
                var=c.get("var"),
                script=c.get("script"),
                script_path=c_script_path,
                timeout=int(c.get("timeout", 30)),
            )
        )

    # Root paths
    input_path = resolve_path(raw.get("input", ""))
    output_log_path = resolve_path(raw.get("output_log", ""))
    output_csv_path = resolve_path(raw.get("output_csv", ""))

    sbc_raw = raw.get("sleep_before_collect")
    try:
        sleep_before_collect = parse_duration(sbc_raw) if sbc_raw is not None else 0.0
    except ValueError:
        sleep_before_collect = 0.0

    cri_raw = raw.get("collect_retry_interval")
    try:
        collect_retry_interval = parse_duration(cri_raw) if cri_raw is not None else 30.0
    except ValueError:
        collect_retry_interval = 30.0

    cr_raw = raw.get("collect_retries")
    try:
        collect_retries = int(cr_raw) if cr_raw is not None else 3
    except (ValueError, TypeError):
        collect_retries = 3

    return Job(
        yaml_path=path,
        yaml_dir=yaml_dir,
        raw_yaml=raw,
        job_hash=file_hash,
        input=input_path or Path(),
        output_log=output_log_path or Path(),
        output_csv=output_csv_path or Path(),
        actions=str(raw.get("actions", "skip")),
        parallel=int(raw.get("parallel", 10)),
        sleep_before_collect=sleep_before_collect,
        collect_retry_interval=collect_retry_interval,
        collect_retries=collect_retries,
        run_timeout=int(raw.get("run_timeout", 1800)),
        invoke=invoke,
        collect=columns,
        pre_run=prechecks,
        post_run=post_runs,
    )


def load_job(yaml_path: str | Path) -> Job:
    """Load and parse job from a YAML file."""
    raw, file_hash = load_yaml_raw(yaml_path)
    return parse_job_from_dict(raw, yaml_path, file_hash)

