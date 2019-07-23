"""Module to run minimal flask app to be used for tests.
"""

from flask import Flask
APP = Flask(__name__)

@APP.route('/')
def hello_world():
    return 'Hello, World!'

@APP.route('/foo')
def foo():
    return 'bar'
