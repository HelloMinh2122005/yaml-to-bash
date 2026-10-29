"""CLI entrypoint for yaml_to_powershell (y2p)."""

from __future__ import annotations

import sys
from y2shared import cli
from y2p.profile import PROFILE


def main(argv: list[str] | None = None) -> int:
    return cli.main(PROFILE, argv=argv)


if __name__ == "__main__":
    sys.exit(main())

