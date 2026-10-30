Introduction
============

The ``ox_ui`` package provides tools for writing user interfaces.

For example, ``ox_ui`` lets you take a command defined using the
``click`` library and convert it to something you can run in a Flask web
server as discussed in the `Click to WTForms <#click-to-wtforms>`__
section.

The ``ox_ui.tkdantic`` package builds a command-line interface, a
tkinter GUI and an XML-RPC client from pydantic models, as discussed in
the `Pydantic models to CLI and GUI <#tkdantic>`__ section.

Click to WTForms
================

The ``ox_ui`` package can convert a ``click`` command into a flask route
using the ``WTForms`` library. This can be convenient both so that you
have a command line interface (CLI) for your functions in addition to a
web interface and also because sometimes it is quicker and easier to
define the CLI interface and auto-generate the web interface.

Imagine you define a function called ``hello_cmd`` via something like:

.. code:: python

   @click.command()
   @click.option('--count', default=1, type=int, help='how many times to say it')
   @click.option('--text', default='hi', type=str, help='what to say')
   def hello_cmd(count, text):
       'say hello'

       result = []
       for i in range(count):
           result.append(text)

       return '\n'.join(result)

You can import ``c2f`` from ``ox_ui.core`` and use it to convert your
``hello_cmd`` into a flask route via something like:

.. code:: python

   from flask import Flask
   from ox_ui.core import c2f

   APP = Flask(__name__)

   @APP.route('/hello', methods=('GET', 'POST'))
   def hello():
       fcmd = c2f.ClickToWTF(hello_cmd)
       result = fcmd.handle_request()
       return result

Once you start your flask web server, you will then have a route that
introspects ``hello_cmd``, creates a web form using the ``WTForms``
library and handles the command.

See examples in the ``tests`` directory for more details.

.. _tkdantic:

Pydantic models to CLI and GUI (tkdantic)
=========================================

The ``ox_ui.tkdantic`` package lets you describe a command's arguments
once, as a pydantic model, and get a command-line interface and a
tkinter GUI for it. Field types, defaults, descriptions and validators
come from the model, so the CLI, the GUI and a server all check the
arguments the same way. Nested models become groups of options and lists
of models become repeatable entries.

You mark the methods of a class with ``@pydantic_method``. Each takes
one argument, a pydantic model:

.. code:: python

   from typing import Literal

   from pydantic import BaseModel, Field

   from ox_ui.tkdantic.inspector import pydantic_method


   class Trip(BaseModel):
       """A trip to plan."""

       city: str = Field(description='Destination city.')
       seats: int = Field(default=1, gt=0, description='Seats to reserve.')
       transport: Literal['train', 'bus'] = 'train'
       dry_run: bool = Field(default=False, description=(
           'Check the trip but do not reserve anything.'))


   class Planner:
       """Commands for a pretend trip planner."""

       @pydantic_method
       def plan_trip(self, params: Trip):
           """Plan a trip."""
           return f'{params.seats} by {params.transport} to {params.city}'

Command line
------------

``click_group_from_cls`` makes a ``click`` group with one command per
marked method and one option per field:

.. code:: python

   from ox_ui.tkdantic.cli_builder import click_group_from_cls

   if __name__ == '__main__':
       click_group_from_cls(Planner, name='planner')()

.. code:: bash

   $ python planner.py plan_trip --city Oslo --seats 5
   5 by train to Oslo
   $ python planner.py plan_trip --city Oslo --seats 0
   Error: 1 validation error for Trip
   seats
     Input should be greater than 0 ...

A nested model's fields become dotted options such as
``--address.city``. A boolean gets an on and an off flag (``--dry-run``
and ``--no-dry-run``). Every command also has these options:

-  ``--params FILE`` starts from the values in a JSON file, such as one
   saved with the GUI's Dump button. Options given on the command line
   replace the file's values.
-  ``--show-params`` prints the final parameters as JSON and exits
   without running the command.
-  ``--rpc-url URL`` sends the command to an XML-RPC server instead of
   running it locally, and waits for its result. ``--rpc-timeout SECS``
   limits the wait. Giving up does not stop the command on the server.

.. code:: bash

   $ cat trip.json
   {"city": "Oslo", "seats": 5, "transport": "bus"}
   $ python planner.py plan_trip --params trip.json --seats 2
   2 by bus to Oslo
   $ python planner.py plan_trip --params trip.json --rpc-url 127.0.0.1:8765
   5 by bus to Oslo

Server and GUI
--------------

A ``@pydantic_method`` also accepts its model as a JSON string, so an
instance of the class can be served with the standard library's XML-RPC
server:

.. code:: python

   from xmlrpc.server import SimpleXMLRPCServer

   server = SimpleXMLRPCServer(('127.0.0.1', 8765), allow_none=True)
   server.register_instance(Planner())
   server.serve_forever()

``MainApp`` is a GUI client for such a server. It shows one button per
command. Each button opens a form with a widget per field, and the
form's Submit button validates the values and sends them to the server
at the URL shown in the window:

.. code:: python

   from ox_ui.tkdantic.builder import MainApp
   from ox_ui.tkdantic.inspector import commands_for_cls

   commands = {c.title: c for c in commands_for_cls(Planner)}
   MainApp(title='Planner', model_commands=commands).mainloop()

The package also has ``StateRunnerWindow``, a GUI for driving
``transitions`` state machines with long-running steps; see
``src/ox_ui/tkdantic/STATE_RUNNER_README.md``.

Other Utilities
===============

A few additional utilites are provided in the
``src/ox_ui/core/decorators.py`` module including a ``watched``
decorator to log the start/end of functions, a ``setup_flask_watch``
function which applies the ``watched`` decorator to allow your routes
using the ``before_request`` and ``teardown_request`` hooks in flask,
and a ``LockFile`` context decorator for easily adding lock files to any
function or context.

Tagging releases (``ox_ui release``)
====================================

Installing ``ox_ui`` also installs an ``ox_ui`` command. Its ``release``
subcommand tags a release of the git repository you run it in and pushes
it:

.. code:: bash

   ox_ui release origin    # tag v<version> and push it to the remote "origin"
   ox_ui release all       # the same for every remote
   ox_ui release           # list the remotes and do nothing

It reads the package version from ``pyproject.toml``: a static
``[project] version``, or for a dynamic version the file named by
``[tool.hatch.version] path`` or by setuptools' ``attr`` or ``file``.
That file must assign ``__version__`` or ``VERSION``. Use
``--version-file PATH`` for other layouts. Then it:

#. requires a version of the form ``X.Y.Z`` that is above the latest
   ``v*`` tag on the branch;
#. requires the ``main`` or ``master`` branch with no uncommitted
   changes to tracked files;
#. checks each chosen remote: its branch must be the same as the local
   one or behind it, and a ``v<version>`` tag it already has must be on
   this commit;
#. shows what it will do and asks first (``--yes`` skips the question);
#. creates the annotated tag ``v<version>`` if needed and pushes the
   branch (fast-forward only) and that one tag to each chosen remote,
   then reads the tag back.

It never forces a push, never moves a tag, and pushes no other branches
or tags. Running it again after a failure skips the steps that are done.

Development
===========

The project uses `uv <https://docs.astral.sh/uv/>`__; ``uv`` is also a
dev dependency, so ``uv sync`` followed by ``.venv/bin/uv`` works
without a separate install.

.. code:: bash

   uv sync                  # create .venv with ox_ui and its dev tools
   xvfb-run -a make test    # run the tests (xvfb-run only without a display)
   make build               # build the sdist and wheel into dist/
   make pypi                # test, build and upload with uv publish
   make release REMOTE=all  # same as: uv run ox_ui release all

The version is ``__version__`` in ``src/ox_ui/__init__.py`` (the only
place it is written; ``pyproject.toml`` reads it). Changes per version
are in ``docs/CHANGELOG.org``.
