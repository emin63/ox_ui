"""Module to run minimal flask app to be used for tests.

You can do something like the following to serve this app
if you want to test/debug manually:

  FLASK_APP=c2f_app.py python3 -m flask run

"""

import datetime

import click
from flask import Flask, url_for

from ox_ui.core import c2f, decorators


APP = Flask(__name__)
APP.config['WTF_CSRF_ENABLED'] = False
decorators.setup_flask_watch(APP)


@click.command()
@click.option('--count', default=1, type=int, help='how many times to say it')
@click.option('--text', default='hi', type=str, help='what to say')
def hello_cmd(count, text):
    'say hello'

    result = []
    for i in range(count):
        result.append(text)

    return '\n'.join(result)


@click.command()
@click.option('--count', default=2, type=int, help='how many times to say it')
@click.option('--when', type=click.DateTime(formats=[
    '%Y-%m-%d', '%m/%d/%Y']), default=datetime.date.today)
@click.option('--text', default='bye', type=str, help='what to say')
@click.option('--also', multiple=True, default=['them'], type=str,
              help='Also say...; can be provided multiple times.')
def goodbye_cmd(count, text, when, also):
    'say bye'

    result = []
    for i in range(count):
        result.append(text)
    if also:
        result.extend(also)
    result.append('\nat %s' % str(when))

    return '\n'.join(result)


@click.command()
@click.option('--datafile', type=click.File('rb'), help=(
    'Data file to read.'))
def count_file_size_cmd(datafile):
    "Count size of file."

    result = len(datafile.read())
    return str(result)


@click.command()
@click.option('--color', type=click.Choice(['red', 'green', 'blue']), help=(
    'Favorite color.'), default='green')
def favorite_color_cmd(color):
    "Tell me your favorite color."

    return f'I like {color} as well.'


@APP.route('/')
def home():
    url = url_for('hello')
    return 'Welcome to test; try the view at <A HREF="%s">%s</A>' % (
        url, url)


@APP.route('/hello', methods=('GET', 'POST'))
def hello():
    fcmd = c2f.ClickToWTF(hello_cmd)
    result = fcmd.handle_request()
    return result


@APP.route('/goodbye', methods=('GET', 'POST'))
def goodbye():
    return c2f.ClickToWTF(goodbye_cmd).handle_request()


@APP.route('/count_file_size', methods=('GET', 'POST'))
def count_file_size():
    return c2f.ClickToWTF(count_file_size_cmd).handle_request()


@APP.route('/favorite_color', methods=('GET', 'POST'))
def favorite_color():
    return c2f.ClickToWTF(favorite_color_cmd).handle_request()
