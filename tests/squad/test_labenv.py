from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
import pytest

from labenv import (
    LabEnv,
    is_lab_subscription,
    load_lab_env,
    validate_csv,
)


def test_load_lab_env(lab_env):
    assert lab_env.subscription_id == "79f7da80-0c64-4e4b-9ed4-316a88142152"
    assert lab_env.resource_group == "rg-lab-10vms"
    assert lab_env.vms == ("lin01", "lin02", "lin03", "win01", "win02")


def test_load_lab_env_missing_key(tmp_path: Path):
    p = tmp_path / "bad.env"
    p.write_text('LAB_SUBSCRIPTION_ID="sub123"\nLAB_RESOURCE_GROUP="rg"\n')
    with pytest.raises(ValueError):
        load_lab_env(str(p))


def test_is_lab_subscription(lab_env):
    assert is_lab_subscription("79F7DA80-0C64-4E4B-9ED4-316A88142152", lab_env)
    assert not is_lab_subscription("Azure for Students", lab_env)  # name empty -> GUID only


def test_validate_csv_ok(tmp_path: Path, lab_env):
    csv_file = tmp_path / "ok.csv"
    csv_file.write_text(
        "vm_name,resource_group,subscription,os\n"
        "lin01,rg-lab-10vms,79f7da80-0c64-4e4b-9ed4-316a88142152,linux\n"
        "win02,rg-lab-10vms,79f7da80-0c64-4e4b-9ed4-316a88142152,windows\n"
    )
    assert validate_csv(str(csv_file), lab_env) == []


def test_validate_csv_normalises(tmp_path: Path, lab_env):
    csv_file = tmp_path / "norm.csv"
    csv_file.write_text(
        "vm_name,resource_group,subscription,os\n"
        " LIN01 , RG-LAB-10VMS , 79F7DA80-0C64-4E4B-9ED4-316A88142152 ,linux\n"
    )
    assert validate_csv(str(csv_file), lab_env) == []


def test_validate_csv_rejects(tmp_path: Path, lab_env):
    csv_file = tmp_path / "bad.csv"
    csv_file.write_text(
        "vm_name,resource_group,subscription,os\n"
        "lin01,rg-lab-10vms,other-subscription-id,linux\n"
        "lin02,rg-tfstate,79f7da80-0c64-4e4b-9ed4-316a88142152,linux\n"
        "lin09,rg-lab-10vms,79f7da80-0c64-4e4b-9ed4-316a88142152,linux\n"
    )
    errors = validate_csv(str(csv_file), lab_env)
    assert len(errors) == 3
    assert errors[0].startswith("row 2:")
    assert errors[1].startswith("row 3:")
    assert errors[2].startswith("row 4:")


def test_check_csv_cli_exit_codes(tmp_path: Path, repo_root: Path):
    good_csv = tmp_path / "good.csv"
    good_csv.write_text(
        "vm_name,resource_group,subscription,os\n"
        "lin01,rg-lab-10vms,79f7da80-0c64-4e4b-9ed4-316a88142152,linux\n"
    )
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text(
        "vm_name,resource_group,subscription,os\n"
        "lin09,rg-lab-10vms,79f7da80-0c64-4e4b-9ed4-316a88142152,linux\n"
    )

    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root / "lab")

    res_good = subprocess.run(
        [sys.executable, str(repo_root / "lab" / "labenv.py"), "check-csv", str(good_csv)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res_good.returncode == 0

    res_bad = subprocess.run(
        [sys.executable, str(repo_root / "lab" / "labenv.py"), "check-csv", str(bad_csv)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res_bad.returncode == 65


def test_toggle_uses_lab_env(stub_az, repo_root: Path):
    bin_dir, calls_file = stub_az
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

    res = subprocess.run(
        ["bash", str(repo_root / "lab" / "toogle_vm.sh"), "status"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0
    calls = calls_file.read_text()
    assert "--resource-group rg-lab-10vms" in calls
    assert "--subscription 79f7da80-0c64-4e4b-9ed4-316a88142152" in calls
