"""Useful decorators.
"""

import uuid
import logging
import time

import wrapt

from flask import request, g


def _start_watch(name, w_type, args, kwargs, show_args=False):
    """Helper function to start watching command

    :param name:    String name of command to watch.

    :param w_type:  String type (e.g., 'function', 'flask_request').

    :param args:    Arguments to command.

    :param kwargs:  Keyword args.

    :param show_args=False:  Whether to show args/kwargs in log.

    ~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-

    :return:  Dictionary of meta data for watched command.

    ~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-

    PURPOSE:  Create dictionary of meta data and log it so we can
              watch a command. Meant to be called by watched decorator.

    """

    w_data = {'w_uuid': str(uuid.uuid4()),  # UUID to help tracking.
              'w_name': name, 'watched': 'start', 'w_type': w_type,
              'start': time.time()}
    if show_args:
        w_data.update(w_args=args, w_kwargs=str(kwargs))
    logging.info('watched_cmd: %s', name, extra=w_data)
    return w_data


def _end_watch(w_data, str_result):
    """Log an error in a watched command.

    :param w_data:    As produced by _start_watch.

    :param str_result:  Result of command as a string.

    ~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-

    :return:  Updated `w_data`.

    ~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-

    PURPOSE:  Log about a watched command ending.
              Helper meant to be called only be `watched` decorator.

    """

    cmd_time = time.time() - w_data['start']
    w_data.update(watched='end', status='ok', w_run_time=cmd_time,
                  w_result=str_result)
    logging.info('watched_cmd_end: ok:%s', w_data['w_name'], extra=w_data)
    return w_data


def _error_watch(w_data, my_problem):
    """Log an error in a watched command.

    :param w_data:    As produced by _start_watch.

    :param my_problem:    Problem description.

    ~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-

    :return:  Updated `w_data`.

    ~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-

    PURPOSE:  Log a warning about an error in a watched command.
              Helper meant to be called only be `watched` decorator.

    """
    cmd_time = time.time() - w_data['start']
    w_data.update(w_error=str(my_problem), status='error',
                  w_run_time=cmd_time, watched='end')
    logging.warning('watched_end_cmd: error:%s', w_data['w_name'],
                    extra=w_data)
    return w_data


@wrapt.decorator
def watched(wrapped, instance, args, kwargs, show_args=True):
    """Decorator to make a command "watched" where we track timing.

    :param wrapped:    Function to wrap.

    :param instance:   Generally ignored (can be instance of class).

    :param args, kwargs:  Arguments passed to wrapped function. If
                          `show_args` is True, then we log these.

    :param show_args=True:  Whether to show args in logs.

    ~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-

    :return:  Same as calling wrapped function.

    ~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-

    PURPOSE:  Decorator to make a "watched" function where we put info
              about start, end, error, args, into logs. This will create
              log messages starting with 'watched_*' and providing an
              `extra` dictionary to logging with useful meta data. You can
              then search your logs for things like 'watched_end_cmd' to
              find info on how the command ran.

              This is especially useful with something like logz.io.
    """
    logging.debug('watched_meta: Ignoring instance %s', instance)
    name = wrapped.__name__
    w_data = _start_watch(name, 'function', args, kwargs, show_args)
    try:
        str_result = '(unknown)'
        result = wrapped(*args, **kwargs)
        try:
            str_result = str(result)
            if len(str_result) > 200:
                str_result = str_result[:195]+'...'
        except Exception as unexpected:  # pylint: disable=broad-except
            logging.error('Ignoring unexpected str conversion exception: %s',
                          unexpected)
        _end_watch(w_data, str_result)
        return result
    except Exception as my_problem:  # pylint: disable=broad-except
        _error_watch(w_data, my_problem)
        raise


def _start_watching_request(req=None, name=None):
    """Start watching a flask request.

Uses the `_start_watch`, `_end_watch` helpers to watch a flask
command. Use `setup_flask_watch` to turn on for your app. See
docs for `setup_flask_watch` for details.
    """
    req = req or request
    name = name or request.endpoint
    w_data = _start_watch(name, 'flask_request', None, None, False)
    g.w_data = w_data  # pylint: disable=assigning-non-slot


def _end_watching_request(req=None):
    """End watching a flask request.

Uses the `_start_watch`, `_end_watch` helpers to watch a flask
command. Use `setup_flask_watch` to turn on for your app. See
docs for `setup_flask_watch` for details.
    """
    req = req or request
    w_data = getattr(g, 'w_data', None)
    if w_data is None:
        logging.error('No w_data member for request: %s', req)
        return
    str_result = ''
    _end_watch(w_data, str_result[:195])


@watched
def setup_flask_watch(app):
    """Take a flask app as input and setup logs to watch requests.

This uses tools similar to the `watched` decorator to watch and log
start/end of a flask command using before_request and teardown_request
hooks.

Basically just call this on your app and then search your logs for
something like `watched_cmd*` to see info about your flask commands.
    """
    app.before_request(_start_watching_request)
    app.teardown_request(_end_watching_request)
