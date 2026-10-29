"""y2p profile definition."""

from __future__ import annotations

from y2shared.profile import Profile
from y2p.compiler import compile_job, compile_collect_script, WRITE_RESULT_PATTERN
from y2p.validate import validate_runcommand_ps

PROFILE = Profile(
    tool="y2p",
    title="yaml_to_powershell",
    os_family="windows",
    command_id="RunPowerShellScript",
    remote_script_name="remote.ps1",
    collect_script_name="collect.ps1",
    write_result_re=WRITE_RESULT_PATTERN,
    compile_remote=compile_job,
    compile_collect=compile_collect_script,
    validate_runcommand=validate_runcommand_ps,
)

