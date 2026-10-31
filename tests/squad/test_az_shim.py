from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
import pytest

from azpolicy import check_az_args

LAB = "/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-lab-10vms"
VM = LAB + "/providers/Microsoft.Compute/virtualMachines"


@pytest.mark.parametrize(
    "argv",
    [
        ["account", "show", "--subscription", "anything", "--query", "id", "-o", "tsv"],
        ["account", "set", "--subscription", "79f7da80-0c64-4e4b-9ed4-316a88142152"],
        [
            "vm",
            "get-instance-view",
            "--subscription",
            "79f7da80-0c64-4e4b-9ed4-316a88142152",
            "-g",
            "rg-lab-10vms",
            "-n",
            "lin01",
            "--query",
            "x",
            "-o",
            "tsv",
        ],
        ["vm", "start", "-g", "RG-LAB-10VMS", "-n", "win02"],
        [
            "vm",
            "deallocate",
            "--ids",
            VM + "/lin01",
            VM + "/win01",
            "--subscription",
            "79f7da80-0c64-4e4b-9ed4-316a88142152",
        ],
        ["vm", "list", "-d", "-g", "rg-lab-10vms", "--query", "[].id", "-o", "tsv"],
        [
            "vm",
            "run-command",
            "invoke",
            "-g",
            "rg-lab-10vms",
            "-n",
            "lin02",
            "--command-id",
            "RunShellScript",
            "--scripts",
            "echo hi",
        ],
        ["rest", "--method", "post", "--url", VM + "/lin01/runCommand?api-version=2024-07-01", "--body", "@b.json"],
        [
            "rest",
            "--method",
            "POST",
            "--url",
            "https://management.azure.com" + VM + "/win01/powerOff?api-version=2024-07-01",
        ],
        ["rest", "--method", "get", "--url", LAB + "/providers/Microsoft.Compute/virtualMachines?api-version=2024-07-01"],
    ],
)
def test_allow_cases(argv, lab_env):
    assert check_az_args(argv, lab_env) is None


@pytest.mark.parametrize(
    "argv",
    [
        ["group", "list"],
        ["vm", "create", "-g", "rg-lab-10vms", "-n", "x"],
        ["vm", "delete", "-g", "rg-lab-10vms", "-n", "lin01"],
        ["vm", "start", "-g", "other-rg", "-n", "lin01"],
        ["vm", "start", "-g", "rg-lab-10vms", "-n", "lin09"],
        [
            "vm",
            "deallocate",
            "--ids",
            VM + "/lin01",
            "/subscriptions/other/resourceGroups/rg-lab-10vms/providers/Microsoft.Compute/virtualMachines/lin01",
        ],
        ["vm", "list"],
        ["account", "set", "--subscription", "00000000-0000-0000-0000-000000000000"],
        ["rest", "--method", "post", "--url", VM + "/lin01/restart?api-version=2024-07-01"],
        ["rest", "--method", "delete", "--url", VM + "/lin01?api-version=2024-07-01"],
        ["rest", "--method", "put", "--url", VM + "/lin01/runCommands/x?api-version=2024-07-01"],
        ["rest", "--method", "get", "--url", "/subscriptions/79f7da80-0c64-4e4b-9ed4-316a88142152/resourceGroups/rg-tfstate"],
        ["storage", "account", "list"],
        ["role", "assignment", "list"],
    ],
)
def test_deny_cases(argv, lab_env):
    assert check_az_args(argv, lab_env) is not None


def test_shim_passes_allowed_call(stub_az, repo_root: Path):
    bin_dir, calls_file = stub_az
    shim_path = repo_root / "lab" / "az-shim" / "az"
    env = dict(os.environ)
    env["Y2_REAL_AZ"] = str(bin_dir / "az")
    env["Y2_LAB_ENV"] = str(repo_root / "lab" / "lab.env")

    res = subprocess.run(
        [sys.executable, str(shim_path), "account", "show", "--query", "id", "-o", "tsv"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0
    assert "account show --query id -o tsv" in calls_file.read_text()


def test_shim_blocks_denied_call(stub_az, repo_root: Path):
    bin_dir, calls_file = stub_az
    shim_path = repo_root / "lab" / "az-shim" / "az"
    env = dict(os.environ)
    env["Y2_REAL_AZ"] = str(bin_dir / "az")
    env["Y2_LAB_ENV"] = str(repo_root / "lab" / "lab.env")

    res = subprocess.run(
        [sys.executable, str(shim_path), "group", "list"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 3
    assert res.stderr.startswith("y2 lab guard: refused:")
    assert calls_file.read_text().strip() == ""


def test_shim_refuses_without_real_az(repo_root: Path):
    shim_path = repo_root / "lab" / "az-shim" / "az"
    env = dict(os.environ)
    env.pop("Y2_REAL_AZ", None)
    env["Y2_LAB_ENV"] = str(repo_root / "lab" / "lab.env")

    res = subprocess.run(
        [sys.executable, str(shim_path), "account", "show"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 3
    assert "refused" in res.stderr.lower()


def test_shim_refuses_self_reference(repo_root: Path):
    shim_path = repo_root / "lab" / "az-shim" / "az"
    env = dict(os.environ)
    env["Y2_REAL_AZ"] = str(shim_path)
    env["Y2_LAB_ENV"] = str(repo_root / "lab" / "lab.env")

    res = subprocess.run(
        [sys.executable, str(shim_path), "account", "show"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 3
    assert "refused" in res.stderr.lower()

