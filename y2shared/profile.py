"""Profile definition for yaml_to_* tools."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Any

from y2shared.config import Job


class Reporter:
    """Error and warning reporting helper passed to frontend validators."""

    def __init__(
        self,
        err_fn: Callable[[str, str], None],
        warn_fn: Callable[[str, str], None],
    ) -> None:
        self._err = err_fn
        self._warn = warn_fn

    def err(self, path: str, msg: str) -> None:
        self._err(path, msg)

    def warn(self, path: str, msg: str) -> None:
        self._warn(path, msg)


@dataclass(frozen=True)
class Profile:
    tool: str                    # "y2b" | "y2p": CLI name in messages ("Rerun with: y2p run … --resume")
    title: str                   # "yaml_to_bash" | "yaml_to_powershell": run banner
    os_family: str               # "linux" | "windows": every CSV row must match
    command_id: str              # "RunShellScript" | "RunPowerShellScript"
    remote_script_name: str      # "remote.sh" | "remote.ps1"
    collect_script_name: str     # "collect.sh" | "collect.ps1"
    write_result_re: re.Pattern  # static scan of step bodies
    compile_remote: Callable[..., tuple[str, list[str]]]   # (job, run_id) -> (script, errors)
    compile_collect: Callable[[Job], tuple[str, list[str]]]
    validate_runcommand: Callable[[dict[str, Any], Path, Reporter], None]   # frontend rules for invoke.runcommand

