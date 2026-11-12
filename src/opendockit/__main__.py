"""`python -m opendockit` — same as the `opendockit` command."""

import sys

from opendockit.cli import main


if __name__ == "__main__":
    sys.exit(main())
