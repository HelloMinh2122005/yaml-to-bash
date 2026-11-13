"""Adversarial edge-case tests for the CLI and platform layer.

Every test asserts one intended behavior of ``adolog``, ``hostenv``,
``shell``, ``repo``, ``azcmd``, and ``aztrace``. Tests marked ``bug``
reproduce a confirmed defect.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from y2shared import hostenv
from y2shared.adolog import ProcessLogSink, is_tf_build, neutralise, print_vm_groups
from y2shared.azcmd import argv_matches, build_run_command_post
from y2shared.aztrace import format_engine_line, redact_argv, redact_text
from y2shared.hostenv import check_host_bash, raise_nofile, resolve_bash
from y2shared.inputcsv import VMRow
from y2shared.repo import find_repo_root, rel_posix
from y2shared.shell import run_bash

GUID = "11111111-1111-1111-1111-111111111111"


def make_row(index: int = 1, vm_name: str = "vm-1") -> VMRow:
    return VMRow(
        index=index,
        vm_name=vm_name,
        resource_group="rg-1",
        subscription="Sub One",
        os="linux",
        raw_data={"vm_name": vm_name},
        env={"VM_NAME": vm_name},
    )


# ---------------------------------------------------------------------------
# adolog.py
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("true", True),
        (" TRUE ", True),
        ("True", True),
        ("1", False),
        ("", False),
        ("false", False),
    ],
)
def test_is_tf_build_accepts_only_true(monkeypatch, value, expected):
    """EP: TF_BUILD is true only for the case-insensitive literal 'true'."""
    monkeypatch.setenv("TF_BUILD", value)
    assert is_tf_build() is expected


def test_neutralise_disables_single_ado_prefixes():
    """branch: the documented prefixes are rewritten so ADO ignores them."""
    assert neutralise("##vso[task.setvariable variable=x]") == "#vso[task.setvariable variable=x]"
    assert neutralise("##[group]name") == "#[group]name"


@pytest.mark.bug
def test_neutralise_disables_stacked_ado_prefixes():
    """EP: '###vso[' must not leave an active '##vso[' prefix after rewriting."""
    out = neutralise("###vso[task.complete result=Failed]")
    assert "##vso[" not in out


def test_process_log_sink_drains_per_index():
    """branch: drain returns the lines once and forgets them."""
    sink = ProcessLogSink()
    sink.add(1, "vm-1", "one")
    sink.add(1, "vm-1", "two")
    sink.add(2, "vm-2", "other")
    assert sink.drain(1) == ["one", "two"]
    assert sink.drain(1) == []
    assert sink.drain(2) == ["other"]


@pytest.mark.bug
def test_print_vm_groups_neutralises_group_header(capsys):
    """EP: a VM name carrying a newline must not inject an ADO command."""
    row = make_row(vm_name="vm-1\n##vso[task.complete result=Failed]")
    sink = ProcessLogSink()
    sink.add(row.index, row.vm_name, "line")

    print_vm_groups([row], sink, lambda r: "OK")

    out = capsys.readouterr().out
    assert not any(line.startswith("##vso[") for line in out.splitlines())


# ---------------------------------------------------------------------------
# hostenv.py
# ---------------------------------------------------------------------------


@pytest.mark.bug
def test_resolve_bash_falls_back_when_y2_bash_is_missing(monkeypatch):
    """branch: a stale Y2_BASH must fall back to PATH instead of crashing."""
    monkeypatch.setenv("Y2_BASH", "/definitely/missing/bash")
    bash_path, (major, minor) = resolve_bash()
    assert Path(bash_path).exists()
    assert (major, minor) >= (3, 2)


def test_check_host_bash_warns_once_per_process(monkeypatch):
    """branch: the host bash warning is emitted at most once per process."""
    monkeypatch.setattr(hostenv, "_bash_warning_emitted", False)
    monkeypatch.setattr(hostenv, "resolve_bash", lambda: ("/bin/bash", (3, 2)))
    warnings = []

    check_host_bash(warn_fn=warnings.append)
    check_host_bash(warn_fn=warnings.append)

    assert len(warnings) == 1
    assert "3.2-compatible" in warnings[0]


def test_raise_nofile_returns_limits():
    """branch: the helper reports the effective soft/hard limits as integers."""
    soft, hard = raise_nofile(required_fds=16)
    assert isinstance(soft, int) and isinstance(hard, int)
    assert soft >= 0


# ---------------------------------------------------------------------------
# shell.py
# ---------------------------------------------------------------------------


def test_run_bash_merges_env_and_honours_cwd(tmp_path):
    """branch: environment overrides are visible and cwd is applied."""
    result = run_bash('printf "%s|%s" "$Y2_EDGE_VALUE" "$PWD"', env={"Y2_EDGE_VALUE": "v"}, cwd=tmp_path)
    assert result.rc == 0
    assert result.stdout == f"v|{tmp_path}"


def test_run_bash_timeout_reports_rc_124_and_flag():
    """BVA: exceeding the timeout yields the documented rc 124 and timed_out flag."""
    result = run_bash("sleep 3", timeout=1)
    assert result.rc == 124
    assert result.timed_out is True
    assert "[TIMEOUT]" in result.stderr


# ---------------------------------------------------------------------------
# repo.py
# ---------------------------------------------------------------------------


def test_find_repo_root_walks_up_to_git_directory(tmp_path):
    """branch: a nested directory resolves to the ancestor holding .git."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    assert find_repo_root(nested) == root.resolve()


def test_find_repo_root_returns_none_outside_any_repo():
    """BVA: walking to the filesystem root without .git returns None."""
    assert find_repo_root(Path("/nonexistent-y2-root")) is None


def test_rel_posix_strips_root_and_keeps_forward_slashes(tmp_path):
    """EP: paths under the root become POSIX-relative."""
    target = tmp_path / "sub" / "file.txt"
    target.parent.mkdir()
    assert rel_posix(target, tmp_path) == "sub/file.txt"


def test_rel_posix_keeps_absolute_path_outside_root(tmp_path):
    """EP: a path outside the root is returned absolute, not as a broken '..' chain."""
    outside = tmp_path.parent / "elsewhere" / "file.txt"
    assert rel_posix(outside, tmp_path) == outside.resolve().as_posix()


# ---------------------------------------------------------------------------
# azcmd.py
# ---------------------------------------------------------------------------


def test_build_run_command_post_percent_encodes_resource_names(tmp_path):
    """EP: resource group and VM names cannot break out of the URL path."""
    argv = build_run_command_post("sub-1", "rg/one", "vm two", tmp_path / "body.json")
    url = argv[argv.index("--url") + 1]
    assert "resourceGroups/rg%2Fone" in url
    assert "virtualMachines/vm%20two" in url


def test_argv_matches_binds_placeholder_on_success():
    """branch: a matching GUID is bound to the placeholder name."""
    bindings = {}
    assert argv_matches(["az", "x", "<id-of:sub>"], ["az", "x", GUID], bindings) is True
    assert bindings == {"sub": GUID}


@pytest.mark.bug
def test_argv_matches_does_not_bind_on_failed_comparison():
    """branch: a rejected candidate must not leave placeholder bindings behind."""
    bindings = {}
    ok = argv_matches(
        ["az", "x", "<id-of:sub>", "--tail", "planned"],
        ["az", "x", GUID, "--tail", "different"],
        bindings,
    )
    assert ok is False
    assert bindings == {}


# ---------------------------------------------------------------------------
# aztrace.py
# ---------------------------------------------------------------------------


def test_redact_argv_masks_secret_flags_in_both_forms():
    """EP: '--flag VALUE' and '--flag=VALUE' are both redacted."""
    argv = ["az", "login", "--password", "hunter2", "--client-secret=abc", "--query", "x"]
    assert redact_argv(argv) == [
        "az",
        "login",
        "--password",
        "***",
        "--client-secret=***",
        "--query",
        "x",
    ]


def test_redact_text_masks_secret_flags_in_free_text():
    """EP: the text form of a secret flag is redacted too."""
    redacted = redact_text("az login --password hunter2 --verbose")
    assert "hunter2" not in redacted
    assert "--password ***" in redacted


def test_format_engine_line_never_contains_secret_values():
    """EP: trace lines are built from the redacted argv."""
    line = format_engine_line("invoke", 1, 0, "SUCCESS", 1.25, ["az", "x", "--token", "abc123"])
    assert "abc123" not in line
    assert "***" in line
    assert line.startswith("engine:invoke attempt=1 rc=0 status=SUCCESS")
