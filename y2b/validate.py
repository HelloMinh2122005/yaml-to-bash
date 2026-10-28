"""Validation engine for yaml_to_bash jobs and CSV input."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from y2b.config import Job, load_yaml_raw, parse_job_from_dict

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


def validate_job_dict(raw: dict[str, Any], yaml_path: str | Path) -> ValidationResult:
    """Validate raw YAML dictionary against all yaml_to_bash schema rules."""
    path = Path(yaml_path).resolve()
    yaml_dir = path.parent
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []

    def err(p: str, msg: str) -> None:
        errors.append(ValidationIssue(path=p, message=msg))

    def warn(p: str, msg: str) -> None:
        warnings.append(ValidationIssue(path=p, message=msg))

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

    # sleep_before_collect
    if "sleep_before_collect" in raw:
        sbc = raw["sleep_before_collect"]
        if not isinstance(sbc, (int, float)) or isinstance(sbc, bool) or sbc < 0:
            err("sleep_before_collect", f"Invalid sleep_before_collect '{sbc}'; must be a number >= 0")

    # run_timeout
    if "run_timeout" in raw:
        rt = raw["run_timeout"]
        if not isinstance(rt, int) or isinstance(rt, bool) or rt <= 0:
            err("run_timeout", f"Invalid run_timeout '{rt}'; must be a positive integer")

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
    step_vars: set[str] = set()
    if not isinstance(rc, dict):
        err("invoke.runcommand", "Missing required 'runcommand' mapping under 'invoke'")
    else:
        res_file = rc.get("res_file")
        if not res_file or not isinstance(res_file, str) or not os.path.isabs(res_file):
            err("invoke.runcommand.res_file", f"res_file must be an absolute path, got '{res_file}'")

        log_file = rc.get("log_file")
        if not log_file or not isinstance(log_file, str) or not os.path.isabs(log_file):
            err("invoke.runcommand.log_file", f"log_file must be an absolute path, got '{log_file}'")

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

                var = s.get("var")
                if not var or not isinstance(var, str):
                    err(f"{s_path}.var", "Step must have a non-empty 'var'")
                else:
                    if not VAR_PATTERN.match(var):
                        err(f"{s_path}.var", f"Variable name '{var}' is invalid (must match ^[A-Z_][A-Z0-9_]*$)")
                    elif var in step_vars:
                        err(f"{s_path}.var", f"Duplicate variable name '{var}'")
                    else:
                        step_vars.add(var)

                has_script = "script" in s and s["script"] is not None
                has_script_path = "script_path" in s and s["script_path"] is not None
                if (has_script and has_script_path) or (not has_script and not has_script_path):
                    err(s_path, "Step must define exactly one of 'script' or 'script_path'")

                if has_script_path:
                    sp = str(s["script_path"])
                    resolved = resolve_path(sp)
                    if not resolved or not resolved.is_file():
                        err(f"{s_path}.script_path", f"Script file not found or not readable: '{sp}'")
                    elif not os.access(resolved, os.X_OK):
                        warn(f"{s_path}.script_path", f"Script file '{sp}' is not executable (harmless since embedded)")

                if "timeout" in s:
                    t = s["timeout"]
                    if not isinstance(t, int) or isinstance(t, bool) or t <= 0:
                        err(f"{s_path}.timeout", f"Invalid timeout '{t}'; must be a positive integer")

    # collect
    collect = raw.get("collect")
    referenced_vars: set[str] = set()
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
                referenced_vars.add(v)
                if v not in step_vars:
                    err(f"{c_path}.var", f"Referenced variable '{v}' is not declared by any runcommand step")
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

    # Warn about unused step vars
    for v in step_vars:
        if v not in referenced_vars:
            warn("invoke.runcommand.steps", f"Variable '{v}' is declared in a step but never used in 'collect'")

    return ValidationResult(errors=errors, warnings=warnings)


def validate_job_file(yaml_path: str | Path) -> tuple[Job | None, ValidationResult]:
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

    res = validate_job_dict(raw, path)
    if not res.is_valid:
        return None, res

    job = parse_job_from_dict(raw, path, file_hash)
    return job, res

