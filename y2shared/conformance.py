"""Plan conformance checking against recorded az trace (AC-13)."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set

from y2shared.azcmd import (
    KIND_DEALLOCATE,
    KIND_INVOKE,
    KIND_POWEROFF,
    KIND_READER,
    argv_matches,
)
from y2shared.aztrace import EngineEntry, TraceRecorder, quote_argv
from y2shared.inputcsv import VMRow
from y2shared.plan import PlanContext, build_vm_plan


@dataclass
class ConformanceReport:
    matched: int
    total: int
    mismatches: Dict[str, str]  # vm_name -> first problem string


def format_summary(report: ConformanceReport) -> str:
    """Format footer summary line per AC-13."""
    return f"conformance: {report.matched}/{report.total} VMs matched plan"


def evaluate(
    ctx: PlanContext,
    work_rows: Sequence[VMRow],
    recorder: TraceRecorder,
    phases_run: Sequence[str],
    expected: Optional[Dict[int, Set[str]]] = None,
) -> ConformanceReport:
    """
    Evaluate recorded az execution against plan for all VMs this process worked on.
    """
    bindings: Dict[str, str] = {}
    mismatches: Dict[str, str] = {}

    active_phases = set(phases_run)
    if "all" in active_phases:
        active_phases = {"invoke", "collect", "post"}

    for row in work_rows:
        # 1. Take recorded engine entries for this VM
        raw_entries = [e for e in recorder.entries(row.index) if isinstance(e, EngineEntry)]

        # 2. Collapse consecutive retry attempts with equal (kind, argv)
        collapsed: List[EngineEntry] = []
        for e in raw_entries:
            if not collapsed:
                collapsed.append(e)
            elif (e.kind, e.argv) != (collapsed[-1].kind, collapsed[-1].argv):
                collapsed.append(e)
            elif collapsed[-1].status == "SUCCESS":
                # Previous execution already succeeded; subsequent entry is a duplicate call
                collapsed.append(e)
            elif e.status == "SUCCESS":
                # Successful retry completes the call, update collapsed entry to SUCCESS
                collapsed[-1] = e
            else:
                # Both are retry/in-progress attempts (e.g. 429 x2, STILL_RUNNING x2)
                pass

        # 3. Plan commands and budget
        vm_plan = build_vm_plan(ctx, row)
        commands = vm_plan.get("commands", {})
        budget = vm_plan.get("budget", [])

        # Filter budget stages by phases_run
        stage_allow: List[Dict[str, Any]] = []
        for s in budget:
            if s["stage"] in active_phases:
                stage_allow.append({
                    "stage": s["stage"],
                    "allow": dict(s.get("allow", {})),
                    "at_most_one_of": list(s.get("at_most_one_of", [])),
                    "used_at_most_one": False,
                })

        stage_idx = 0
        problem: Optional[str] = None

        for entry in collapsed:
            # Command check
            if entry.kind not in commands:
                problem = f"unexpected {entry.kind} :: {quote_argv(entry.argv)}"
                break

            if not argv_matches(commands[entry.kind], entry.argv, bindings):
                problem = f"unexpected {entry.kind} :: {quote_argv(entry.argv)}"
                break

            # Stage / budget walk
            found_stage = False
            for idx in range(stage_idx, len(stage_allow)):
                st = stage_allow[idx]

                # Cannot skip past invoke until invoke is consumed
                if stage_allow[stage_idx]["stage"] == "invoke" and idx > stage_idx:
                    break

                # From collect, can only skip to post if kind is a post-only power kind
                if stage_allow[stage_idx]["stage"] == "collect" and idx > stage_idx:
                    if entry.kind not in (KIND_DEALLOCATE, KIND_POWEROFF):
                        break

                if st["allow"].get(entry.kind, 0) > 0:
                    if entry.kind in st["at_most_one_of"] and st["used_at_most_one"]:
                        continue

                    # Can charge this stage
                    found_stage = True
                    st["allow"][entry.kind] -= 1
                    if entry.kind in st["at_most_one_of"]:
                        st["used_at_most_one"] = True

                    if st["stage"] == "invoke":
                        if entry.kind == KIND_INVOKE:
                            stage_idx = idx + 1
                        else:
                            stage_idx = idx
                    elif st["stage"] == "collect":
                        stage_idx = idx
                    elif st["stage"] == "post":
                        stage_idx = idx
                    break

            if not found_stage:
                problem = f"unexpected {entry.kind} :: {quote_argv(entry.argv)}"
                break

        # 4. Check expected marks if no previous problem
        if problem is None:
            expected_kinds = set(expected.get(row.index, set())) if expected is not None else set()
            expected_kinds.update(recorder.expected(row.index))

            for exp_kind in expected_kinds:
                if not any(e.kind == exp_kind for e in collapsed):
                    exp_argv = commands.get(exp_kind)
                    if exp_argv:
                        problem = f"missing {exp_kind} :: {quote_argv(exp_argv)}"
                    else:
                        problem = f"missing {exp_kind}"
                    break

        if problem is not None:
            mismatches[row.vm_name] = problem

    matched = len(work_rows) - len(mismatches)
    return ConformanceReport(matched=matched, total=len(work_rows), mismatches=mismatches)
