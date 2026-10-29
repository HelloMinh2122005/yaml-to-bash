"""CLI entrypoint for yaml_to_bash."""

from __future__ import annotations

import sys
from y2shared import cli
from y2b.profile import PROFILE


def main(argv: list[str] | None = None) -> int:
    return cli.main(PROFILE, argv=argv)


if __name__ == "__main__":
    sys.exit(main())
