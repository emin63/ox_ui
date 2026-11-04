"""Simple tools for making an RPC client.
"""

from collections.abc import Callable
import datetime
from logging import getLogger
import threading
import traceback
from typing import Optional
import xmlrpc.client
from xmlrpc.client import Transport

# Each thread gets its storage space
THREAD_LOCALS = threading.local()

LOGGER = getLogger(__name__)


class TimeoutTransport(Transport):
    """Create a custom Transport that has a timeout.
    """
    def __init__(self, timeout, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self.timeout
        if conn.sock is not None:
            conn.sock.settimeout(self.timeout)
        return conn


class SingleTryTransport(TimeoutTransport):
    """A :class:`TimeoutTransport` that sends each request once.

    ``xmlrpc.client.Transport`` sends a request again when the
    connection drops (meant for a stale kept-alive connection),
    even after the server got the first copy.  For a command that
    starts a job, a second copy starts a second job.
    """

    def request(self, host, handler, request_body, verbose=False):
        """Send the request once; any failure is raised."""
        return self.single_request(host, handler, request_body,
                                   verbose)


def single_try_proxy(url, timeout=None, use_builtin_types=True):
    """A new ``ServerProxy`` for *url* that sends each request once.

    *timeout* is in seconds; ``None`` waits until the server
    answers.  Unlike :func:`get_proxy` the proxy is not cached:
    each call gets its own connection, so the resend that
    :class:`SingleTryTransport` drops could never have helped.
    """
    return xmlrpc.client.ServerProxy(
        url, allow_none=True, use_builtin_types=use_builtin_types,
        transport=SingleTryTransport(
            timeout=timeout, use_builtin_types=use_builtin_types))


def get_proxy(url, *args, timeout=30, use_builtin_types=True, **kwargs):
    """Return xmlrpc.client.ServerProxy using given timeout in transport.

    :param url:   URL to connect to.

    :param *args:  Passed to xmlrpc.client.ServerProxy.

    :param timeout=30:  Timeout in seconds.

    :param **kwargs:  Passed to xmlrpc.client.ServerProxy.

    ~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-

    :return:  Instance of xmlrpc.client.ServerProxy for given url/timeout/etc

    ~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-

    PURPOSE:  Create a proxy for desired settings. We cache these per
              thread so that you can get a different proxy for each
              desired timeout but we mitigate performance hit of redoing
              setup by caching proxies.

    """
    key = f"{url}_{timeout}_{args=}_{kwargs=}_{use_builtin_types=}"
    if not hasattr(THREAD_LOCALS, 'proxies'):
        THREAD_LOCALS.proxies = {}
    if key not in THREAD_LOCALS.proxies:
        THREAD_LOCALS.proxies[key] = xmlrpc.client.ServerProxy(
            url, *args, transport=TimeoutTransport(
                timeout=timeout, use_builtin_types=use_builtin_types),
            **kwargs
        )
    return THREAD_LOCALS.proxies[key]


class SimpleRPCCall(threading.Thread):
    """Sub-class of thread to do an xmlrpc call.

    This class sub-classes thread so we can make an xmlrpc call inside
    a thread and then possibly execute a callback after finishing. This
    is useful for building a GUI client to make RPC calls.
    """


    def __init__(self, url, command_name, cmd_args, after=None,
                 rpc_timeout=30, daemon=True,
                 log_func: Optional[Callable[[str], None]] = None, **kwargs):
        """Initializer.

        :param url:  URL of server.

        :param command_name:  Name of remote command to run.

        :param cmd_args:  Argument to remote command.

        :param after=None:  Optional object with fields function, args,
                            kwargs, and interval indicating a function
                            to call after finishing the run method.

        :param rpc_timeout=30:  Default timeout to allow for RPC call.

        :param daemon=True:  Whether the thread is daemonic.

        :param log_func=None:  Optional callable which takes a string
                               log message. If None, then we use an
                               internal info logging call.

        :param **kwargs:  Passed to threading.Thread.__init__.

        """
        super().__init__(daemon=daemon, **kwargs)
        self.url = url
        self.rpc_timeout = rpc_timeout
        self.command_name = command_name
        self.cmd_args = cmd_args
        self.after = after
        self.result = None
        self.log_func = log_func or LOGGER.info

    def run(self):
        """Do RPC call, put result into self.result, and do callback.
        """
        try:
            start = datetime.datetime.now()
            # Sent once: a resend after a dropped connection could
            # run the command twice on the server.
            with single_try_proxy(self.url,
                                  timeout=self.rpc_timeout) as proxy:
                method = getattr(proxy, self.command_name)
                if isinstance(self.cmd_args, list):
                    self.result = method(*self.cmd_args)
                elif hasattr(self.cmd_args, 'model_dump_json'):
                    self.result = method(self.cmd_args.model_dump_json())
                else:
                    self.result = method(self.cmd_args)
            finish = datetime.datetime.now()
            duration = finish-start
            msg = f'Did {self.command_name} in {duration.total_seconds()}'
            self.log_func(msg)
        except Exception:  # pylint: disable=broad-except
            LOGGER.exception('Problem in running command %s',
                             self.command_name)
            self.result = traceback.format_exc()
        if self.after:
            kwargs = dict(self.after.kwargs) if self.after.kwargs else {}
            after_thread = threading.Timer(
                self.after.interval, self.after.function, self.after.args,
                kwargs)
            after_thread.start()
