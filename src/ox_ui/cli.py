"""The ``ox_ui`` command line tool.

Subcommands:

- ``release``: tag a release and push it (see :mod:`ox_ui.release`).
"""

import click

from ox_ui import __version__
from ox_ui.release import release


@click.group()
@click.version_option(__version__, prog_name='ox_ui')
def main() -> None:
    """Tools from the ox_ui package."""


main.add_command(release)


if __name__ == '__main__':
    main()
