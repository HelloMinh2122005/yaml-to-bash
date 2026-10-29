"""y2b profile definition."""

from __future__ import annotations

from y2shared.profile import Profile
from y2b.compiler import compile_job, compile_collect_script, WRITE_RESULT_PATTERN
from y2b.validate import validate_runcommand_bash

PROFILE = Profile(
    tool="y2b",
    title="yaml_to_bash",
    os_family="linux",
    command_id="RunShellScript",
    remote_script_name="remote.sh",
    collect_script_name="collect.sh",
    write_result_re=WRITE_RESULT_PATTERN,
    compile_remote=compile_job,
    compile_collect=compile_collect_script,
    validate_runcommand=validate_runcommand_bash,
)

