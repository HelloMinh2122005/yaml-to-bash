"""Validation engine for yaml_to_* jobs and CSV input."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from y2shared.config import Job, load_yaml_raw, parse_job_from_dict, parse_duration
from y2shared.profile import Profile, Reporter
from y2shared.scan import scan_written_vars

VAR_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")
INPUT_SOURCE_PATTERN = re.compile(r"^input\.(.+)$")


@dataclass
class ValidationIssue:
    path: str
    message: str

    def __str__(self) -> str:
        if self.path:
            return f"{self.path}: {self.message}"
        return self.message


@dataclass
class ValidationResult:
    errors: list[ValidationIssue]
    warnings: list[ValidationIssue]

    @property
    def is_valid(self) -> bool:
        return len(self.errors) == 0


def validate_job_dict(
    raw: dict[str, Any],
    yaml_path: str | Path,
    profile: Profile | None = None,
) -> ValidationResult:
    """Validate raw YAML dictionary against all yaml_to_* schema rules."""
    if profile is None:
        from y2b.profile import PROFILE
        profile = PROFILE

    path = Path(yaml_path).resolve()
    yaml_dir = path.parent
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []

    def err(p: str, msg: str) -> None:
        errors.append(ValidationIssue(path=p, message=msg))

    def warn(p: str, msg: str) -> None:
        warnings.append(ValidationIssue(path=p, message=msg))

    reporter = Reporter(err, warn)

    def resolve_path(p: str | None) -> Path | None:
        if not p:
            return None
        cand = Path(p)
        if cand.is_absolute():
            return cand
        return (yaml_dir / cand).resolve()

    # Root requirements
    for req_field in ["input", "output_csv", "output_log"]:
        val = raw.get(req_field)
        if not val or not isinstance(val, str) or not val.strip():
            err(req_field, f"Missing required field '{req_field}'")

    # actions
    if "actions" in raw:
        actions = str(raw["actions"])
        if actions not in ("skip", "start"):
            err("actions", f"Invalid actions policy '{actions}'; must be 'skip' or 'start'")

    # parallel
    if "parallel" in raw:
        par = raw["parallel"]
        if not isinstance(par, int) or isinstance(par, bool) or par < 1 or par > 100:
            err("parallel", f"Invalid parallel '{par}'; must be an integer between 1 and 100")

    # sleep_before_collect (required)
    sbc_seconds = None
    if "sleep_before_collect" not in raw:
        err("sleep_before_collect", "Missing required field 'sleep_before_collect'")
    else:
        try:
            sbc_seconds = parse_duration(raw["sleep_before_collect"])
        except ValueError as e:
            err("sleep_before_collect", f"Invalid sleep_before_collect '{raw['sleep_before_collect']}': {e}")

    # collect_retry_interval (optional, default 30s)
    cri_seconds = 30.0
    if "collect_retry_interval" in raw:
        try:
            cri_val = parse_duration(raw["collect_retry_interval"])
            if cri_val <= 0:
                err("collect_retry_interval", f"Invalid collect_retry_interval '{raw['collect_retry_interval']}'; must be > 0")
            else:
                cri_seconds = cri_val
        except ValueError as e:
            err("collect_retry_interval", f"Invalid collect_retry_interval '{raw['collect_retry_interval']}': {e}")

    # collect_retries (optional, default 3)
    cr_count = 3
    if "collect_retries" in raw:
        cr_raw = raw["collect_retries"]
        if not isinstance(cr_raw, int) or isinstance(cr_raw, bool) or cr_raw < 0:
            err("collect_retries", f"Invalid collect_retries '{cr_raw}'; must be an integer >= 0")
        else:
            cr_count = cr_raw

    # run_timeout (deprecated)
    if "run_timeout" in raw:
        warn("run_timeout", "'run_timeout' is deprecated and ignored; collect timing is controlled by sleep_before_collect, collect_retry_interval, and collect_retries")

    # invoke section
    invoke = raw.get("invoke")
    if not isinstance(invoke, dict):
        err("invoke", "Missing required 'invoke' mapping")
        return ValidationResult(errors, warnings)

    # invoke.precheck
    prechecks = invoke.get("precheck")
    if prechecks is not None:
        if not isinstance(prechecks, list):
            err("invoke.precheck", "'precheck' must be a list")
        else:
            seen_precheck_names: set[str] = set()
            for i, p in enumerate(prechecks):
                p_path = f"invoke.precheck[{i}]"
                if not isinstance(p, dict):
                    err(p_path, "Precheck item must be a mapping")
                    continue

                name = p.get("name")
                if not name or not isinstance(name, str):
                    err(f"{p_path}.name", "Precheck must have a non-empty 'name'")
                else:
                    if name in seen_precheck_names:
                        err(f"{p_path}.name", f"Duplicate precheck name '{name}'")
                    seen_precheck_names.add(name)

                has_script = "script" in p and p["script"] is not None
                has_script_path = "script_path" in p and p["script_path"] is not None
                if (has_script and has_script_path) or (not has_script and not has_script_path):
                    err(p_path, "Precheck must define exactly one of 'script' or 'script_path'")

                if has_script_path:
                    sp = str(p["script_path"])
                    resolved = resolve_path(sp)
                    if not resolved or not resolved.is_file():
                        err(f"{p_path}.script_path", f"Script file not found or not readable: '{sp}'")
                    elif not os.access(resolved, os.X_OK):
                        warn(f"{p_path}.script_path", f"Script file '{sp}' is not executable (harmless since embedded)")

                if "timeout" in p:
                    t = p["timeout"]
                    if not isinstance(t, int) or isinstance(t, bool) or t <= 0:
                        err(f"{p_path}.timeout", f"Invalid timeout '{t}'; must be a positive integer")

                if "cache" in p:
                    cache_val = p["cache"]
                    if cache_val != "subscription":
                        err(f"{p_path}.cache", f"Invalid cache '{cache_val}'; must be 'subscription'")

                # result mapping
                result = p.get("result")
                if not isinstance(result, dict) or not result:
                    err(f"{p_path}.result", "Precheck must define a non-empty 'result' mapping")
                else:
                    for r_key, r_spec in result.items():
                        r_key_str = str(r_key)
                        res_p = f"{p_path}.result[{r_key_str}]"
                        if not isinstance(r_spec, dict):
                            err(res_p, "Result entry must be a mapping with 'log' and 'action'")
                            continue
                        log_val = r_spec.get("log")
                        if log_val is None or not isinstance(log_val, str):
                            err(f"{res_p}.log", "Result entry must define a 'log' string")
                        action_val = r_spec.get("action")
                        if action_val not in ("continue", "skip", "end_check"):
                            err(f"{res_p}.action", f"Invalid action '{action_val}'; must be 'continue', 'skip', or 'end_check'")

    # invoke.runcommand
    rc = invoke.get("runcommand")
    var_to_steps: dict[str, list[int]] = {}
    if not isinstance(rc, dict):
        err("invoke.runcommand", "Missing required 'runcommand' mapping under 'invoke'")
    else:
        profile.validate_runcommand(rc, yaml_dir, reporter)

        steps = rc.get("steps")
        if not isinstance(steps, list) or len(steps) == 0:
            err("invoke.runcommand.steps", "runcommand must define a non-empty list of 'steps'")
        else:
            seen_step_names: set[str] = set()
            for i, s in enumerate(steps):
                s_path = f"invoke.runcommand.steps[{i}]"
                if not isinstance(s, dict):
                    err(s_path, "Step must be a mapping")
                    continue

                name = s.get("name")
                if not name or not isinstance(name, str):
                    err(f"{s_path}.name", "Step must have a non-empty 'name'")
                else:
                    if name in seen_step_names:
                        err(f"{s_path}.name", f"Duplicate step name '{name}'")
                    seen_step_names.add(name)

                if "var" in s:
                    err(f"{s_path}.var", "remove 'var'; use 'write_result NAME VALUE'")

                has_script = "script" in s and s["script"] is not None
                has_script_path = "script_path" in s and s["script_path"] is not None
                if (has_script and has_script_path) or (not has_script and not has_script_path):
                    err(s_path, "Step must define exactly one of 'script' or 'script_path'")

                step_body = ""
                if has_script_path:
                    sp = str(s["script_path"])
                    resolved = resolve_path(sp)
                    if not resolved or not resolved.is_file():
                        err(f"{s_path}.script_path", f"Script file not found or not readable: '{sp}'")
                    else:
                        if not os.access(resolved, os.X_OK):
                            warn(f"{s_path}.script_path", f"Script file '{sp}' is not executable (harmless since embedded)")
                        try:
                            with open(resolved, "r", encoding="utf-8") as sf:
                                step_body = sf.read()
                        except Exception:
                            step_body = ""
                elif has_script:
                    step_body = str(s["script"] or "")

                written = scan_written_vars(step_body, pattern=profile.write_result_re)
                for v in written:
                    var_to_steps.setdefault(v, []).append(i)

                if "timeout" in s:
                    t = s["timeout"]
                    if not isinstance(t, int) or isinstance(t, bool) or t <= 0:
                        err(f"{s_path}.timeout", f"Invalid timeout '{t}'; must be a positive integer")

            for v_name, step_indices in var_to_steps.items():
                if len(step_indices) > 1:
                    warn("invoke.runcommand.steps", f"Variable '{v_name}' is written by multiple steps (last write wins)")

            sum_step_timeouts = 0
            for s in steps:
                if isinstance(s, dict):
                    t = s.get("timeout", 120)
                    if isinstance(t, int) and not isinstance(t, bool) and t > 0:
                        sum_step_timeouts += t

            if sbc_seconds is not None:
                max_wait_time = sbc_seconds + (cr_count * cri_seconds)
                if sum_step_timeouts > max_wait_time:
                    warn(
                        "invoke.runcommand.steps",
                        f"Sum of step timeouts ({sum_step_timeouts}s) exceeds total collect wait time ({max_wait_time:.0f}s = sleep_before_collect + collect_retries * collect_retry_interval)",
                    )

            if sum_step_timeouts > 5400:
                warn(
                    "invoke.runcommand.steps",
                    f"Sum of step timeouts ({sum_step_timeouts}s) exceeds Azure's 90-minute (5400s) run-command cap",
                )

    # collect
    collect = raw.get("collect")
    if not isinstance(collect, list) or len(collect) == 0:
        err("collect", "Missing or empty 'collect' list")
    else:
        seen_columns: set[str] = set()
        for i, c in enumerate(collect):
            c_path = f"collect[{i}]"
            if not isinstance(c, dict):
                err(c_path, "Column item must be a mapping")
                continue

            col_name = c.get("column")
            if not col_name or not isinstance(col_name, str):
                err(f"{c_path}.column", "Column must define a non-empty 'column' name")
            else:
                if col_name in seen_columns:
                    err(f"{c_path}.column", f"Duplicate column name '{col_name}'")
                seen_columns.add(col_name)

            sources = [k for k in ("source", "var", "script", "script_path") if k in c and c[k] is not None]
            if len(sources) != 1:
                err(c_path, f"Column must define exactly one of 'source', 'var', 'script', or 'script_path', got {sources}")
                continue

            src_type = sources[0]
            if src_type == "var":
                v = str(c["var"])
                if not VAR_PATTERN.match(v):
                    err(f"{c_path}.var", f"Variable name '{v}' is invalid (must match ^[A-Z_][A-Z0-9_]*$)")
                elif v.startswith("__Y2B_"):
                    err(f"{c_path}.var", f"Variable name '{v}' is reserved (cannot start with __Y2B_)")
                elif v not in var_to_steps:
                    warn(f"{c_path}.var", f"Referenced variable '{v}' is not written by any runcommand step (static check; ignore if set dynamically)")
            elif src_type == "source":
                s = str(c["source"])
                if not INPUT_SOURCE_PATTERN.match(s):
                    err(f"{c_path}.source", f"Invalid source '{s}'; must match 'input.<field>'")
            elif src_type == "script_path":
                sp = str(c["script_path"])
                resolved = resolve_path(sp)
                if not resolved or not resolved.is_file():
                    err(f"{c_path}.script_path", f"Script file not found or not readable: '{sp}'")
                elif not os.access(resolved, os.X_OK):
                    warn(f"{c_path}.script_path", f"Script file '{sp}' is not executable (harmless since embedded)")

            if "timeout" in c:
                t = c["timeout"]
                if not isinstance(t, int) or isinstance(t, bool) or t <= 0:
                    err(f"{c_path}.timeout", f"Invalid timeout '{t}'; must be a positive integer")

    return ValidationResult(errors=errors, warnings=warnings)


def validate_job_file(
    yaml_path: str | Path,
    profile: Profile | None = None,
) -> tuple[Job | None, ValidationResult]:
    """Validate a YAML file path and parse into Job if valid."""
    path = Path(yaml_path).resolve()
    if not path.is_file():
        return None, ValidationResult(
            errors=[ValidationIssue(path="", message=f"YAML file not found: {path}")],
            warnings=[],
        )

    try:
        raw, file_hash = load_yaml_raw(path)
    except Exception as e:
        return None, ValidationResult(
            errors=[ValidationIssue(path="", message=f"Failed to read/parse YAML: {e}")],
            warnings=[],
        )

    res = validate_job_dict(raw, path, profile=profile)
    if not res.is_valid:
        return None, res

    job = parse_job_from_dict(raw, path, file_hash)
    return job, res

