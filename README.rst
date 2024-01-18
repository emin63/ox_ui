Introduction
============

The ``ox_ui`` package provides tools for writing user interfaces.

For example, ``ox_ui`` lets you take a command defined using the
``click`` library and convert it to something you can run in a Flask web
server as discussed in the `Click to WTForms <#click-to-wtforms>`__
section.

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

Other Utilities
===============

A few additional utilites are provided in the
``ox_ui/core/decorators.py`` module including a ``watched`` decorator to
log the start/end of functions, a ``setup_flask_watch`` function which
applies the ``watched`` decorator to allow your routes using the
``before_request`` and ``teardown_request`` hooks in flask, and a
``LockFile`` context decorator for easily adding lock files to any
function or context.
