"""CLI framework for yaml_to_* tools."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from y2shared.hostenv import check_host_bash
from y2shared.inputcsv import read_input_csv
from y2shared.profile import Profile
from y2shared.runner import run_job
from y2shared.validate import validate_job_file


def cmd_validate(args: argparse.Namespace, profile: Profile) -> int:
    """Validate job YAML configuration and input CSV."""
    check_host_bash()
    yaml_path = Path(args.job_yaml).resolve()
    print(f"Validating job specification: {yaml_path}")

    job, result = validate_job_file(yaml_path, profile=profile)

    has_errors = not result.is_valid
    for w in result.warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    for e in result.errors:
        print(f"ERROR: {e}", file=sys.stderr)

    if has_errors or job is None:
        print(f"\nValidation failed with {len(result.errors)} error(s).", file=sys.stderr)
        return 1

    # Now validate input CSV
    print(f"Validating input CSV: {job.input}")
    rows, headers, csv_result = read_input_csv(job.input, job=job, os_family=profile.os_family)

    for w in csv_result.warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    for e in csv_result.errors:
        print(f"ERROR: {e}", file=sys.stderr)

    if not csv_result.is_valid:
        print(f"\nCSV validation failed with {len(csv_result.errors)} error(s).", file=sys.stderr)
        return 1

    print(f"\nValidation successful!")
    print(f"  Job: {yaml_path.name} (SHA256: {job.job_hash[:12]}...)")
    print(f"  Target VMs: {len(rows)} from {job.input.name}")
    phases_count = {"invoke": 0, "collect": 0}
    for p in job.pre_run:
        for ph in p.phases:
            if ph in phases_count:
                phases_count[ph] += 1
    print(f"  Pre_run checks: {len(job.pre_run)} (invoke: {phases_count['invoke']}, collect: {phases_count['collect']})")
    print(f"  Runcommand steps: {len(job.invoke.runcommand.steps)}")
    print(f"  Collect columns: {len(job.collect)}")
    print(f"  Post_run hooks: {len(job.post_run)}")
    return 0


def cmd_compile(args: argparse.Namespace, profile: Profile) -> int:
    """Compile remote runcommand payload."""
    yaml_path = Path(args.job_yaml).resolve()
    job, result = validate_job_file(yaml_path, profile=profile)
    if not result.is_valid or job is None:
        for e in result.errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    script, errors = profile.compile_remote(job)
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.output:
        out_path = Path(args.output).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(script)
        print(f"Compiled remote script written to: {out_path}")
    else:
        sys.stdout.write(script if script.endswith("\n") else script + "\n")
    return 0


def cmd_run(args: argparse.Namespace, profile: Profile) -> int:
    """Execute job across target VMs."""
    return run_job(args, profile=profile)


def build_parser(profile: Profile) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=profile.tool,
        description=f"{profile.title}: Declarative Azure VM Automation Framework",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # validate
    p_validate = subparsers.add_parser("validate", help="Validate job YAML and input CSV")
    p_validate.add_argument("job_yaml", help="Path to job YAML file")
    p_validate.set_defaults(func=lambda args: cmd_validate(args, profile))

    # compile
    p_compile = subparsers.add_parser(
        "compile", help=f"Compile remote runcommand {profile.tool} payload"
    )
    p_compile.add_argument("job_yaml", help="Path to job YAML file")
    p_compile.add_argument("-o", "--output", help="Write compiled script to file instead of stdout")
    p_compile.set_defaults(func=lambda args: cmd_compile(args, profile))

    # run
    p_run = subparsers.add_parser("run", help="Run automation job across VMs")
    p_run.add_argument("job_yaml", help="Path to job YAML file")
    p_run.add_argument("--vm", action="append", default=[], help="Filter by VM name (repeatable)")
    p_run.add_argument("--limit", type=int, default=None, help="Limit number of VMs to run")
    p_run.add_argument("--parallel", type=int, default=None, help="Override parallel concurrency")
    p_run.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform prechecks and compilation without executing run-command",
    )
    p_run.add_argument(
        "--resume", action="store_true", help="Resume interrupted run using existing parts folder"
    )
    p_run.add_argument(
        "--keep-parts", action="store_true", help="Keep temporary parts folder after merge"
    )
    p_run.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    p_run.set_defaults(func=lambda args: cmd_run(args, profile))

    return parser


def main(profile: Profile, argv: list[str] | None = None) -> int:
    parser = build_parser(profile)
    args = parser.parse_args(argv)
    return args.func(args)

