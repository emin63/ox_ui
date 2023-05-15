"""Useful decorators.
"""

import uuid
import logging
import time

import wrapt


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
    w_data = {'w_uuid': str(uuid.uuid4()),  # UUID to help tracking.
              'w_name': name, 'watched': 'start'}
    if show_args:
        w_data.update(w_args=args, w_kwargs=str(kwargs))
    logging.info('watched_cmd:%s', name, extra=w_data)
    start, cmd_time, result = time.time(), None, None
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
        cmd_time = time.time() - start
        w_data.update(watched='end', status='ok', w_run_time=cmd_time,
                      w_result=str_result)
        logging.info('watched_end_cmd:ok:%s', name, extra=w_data)
        return result
    except Exception as my_problem:  # pylint: disable=broad-except
        cmd_time = time.time() - start
        w_data.update(w_error=str(my_problem), status='error',
                      w_run_time=cmd_time, watched='end')
        logging.warning('watched_end_cmd:error:%s', name, extra=w_data)
        raise
