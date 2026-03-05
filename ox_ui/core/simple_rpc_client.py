"""Simple tools for making an RPC client.
"""

import datetime
from logging import getLogger
import threading
import traceback
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


def get_proxy(url, *args, timeout=30, **kwargs):
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
    key = f"{url}_{timeout}_{args=}_{kwargs=}"
    if not hasattr(THREAD_LOCALS, 'proxies'):
        THREAD_LOCALS.proxies = {}
    if key not in THREAD_LOCALS.proxies:
        THREAD_LOCALS.proxies[key] = xmlrpc.client.ServerProxy(
            url, *args, transport=TimeoutTransport(timeout=timeout), **kwargs
        )
    return THREAD_LOCALS.proxies[key]


class SimpleRPCCall(threading.Thread):
    """Sub-class of thread to do an xmlrpc call.

    This class sub-classes thread so we can make an xmlrpc call inside
    a thread and then possibly execute a callback after finishing. This
    is useful for building a GUI client to make RPC calls.
    """


    def __init__(self, url, command_name, cmd_args, after=None,
                 rpc_timeout=30, daemon=True, **kwargs):
        """Initializer.

        :param url:  URL of server.

        :param command_name:  Name of remote command to run.

        :param cmd_args:  Argument to remote command.

        :param after=None:  Optional object with fields function, args,
                            kwargs, and interval indicating a function
                            to call after finishing the run method.

        :param rpc_timeout=30:  Default timeout to allow for RPC call.

        :param daemon=True:  Whether the thread is daemonic.

        :param **kwargs:  Passed to threading.Thread.__init__.

        """
        super().__init__(daemon=daemon, **kwargs)
        self.url = url
        self.rpc_timeout = rpc_timeout
        self.command_name = command_name
        self.cmd_args = cmd_args
        self.after = after
        self.result = None

    def run(self):
        """Do RPC call, put result into self.result, and do callback.
        """
        try:
            start = datetime.datetime.now()
            proxy = get_proxy(
                self.url, timeout=self.rpc_timeout, allow_none=True)
            method = getattr(proxy, self.command_name)
            if isinstance(self.cmd_args, list):
                self.result = method(*self.cmd_args)
            elif hasattr(self.cmd_args, 'model_dump_json'):
                self.result = method(self.cmd_args.model_dump_json())
            else:
                self.result = method(self.cmd_args)
            finish = datetime.datetime.now()
            duration = finish-start
            LOGGER.info('Finished %s in %s seconds', self.command_name,
                        duration.total_seconds())
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
