"""Tools to help test servers.
"""

import pathlib
import logging
import subprocess
import socket


class ServerInfo:
    """Simple class to hold basic server information.
    """

    def __init__(self, s_proc, s_port):
        self.s_proc = s_proc
        self.s_port = s_port


def find_free_port():
    "Find and return a free port number"

    result = None
    my_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        my_sock.bind(('', 0))
        my_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        result = my_sock.getsockname()[1]
    finally:
        my_sock.close()
    return result


def run_cmd(cmd, cwd=None, timeout=30, env=None):
    """Helper to run a command in a subprocess.

    :param cmd:   List of strings for commands to run.

    :param cwd=None:    Optional current working directory string.

    :param timeout=30:  Timeout in seconds before killing command.

    ~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-

    :return:  The subprocess created to run the command.

    ~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-

    PURPOSE:  Provides some convenience for callnig subprocess.Popen
              to run subprocess commands.

    """
    logging.info('Running cmd: %s', str(cmd))
    proc = subprocess.Popen(cmd, cwd=cwd, env=env)
    if timeout is not None:
        try:
            result = proc.wait(timeout=abs(timeout))
        except subprocess.TimeoutExpired:
            if timeout < 0:
                result = proc.returncode  # timeout is OK
            else:
                raise  # timeout not ok so re-raise it
    else:
        result = proc.returncode
    if result:
        raise ValueError('Error code "%s" in cmd "%s"' % (
            result, proc))
    return proc


def start_server(cmd: list, cwd: str = None, s_port=None, env=None):
    """Start flask server.

    :param cmd:   Command to run to start server.

    :param cwd:      Current working directory to run in.

    :param s_port=None:    Optional port for server. If None, we choose.

    ~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-

    :return:  Instance of ServerInfo containing information about the
              server we started.

    ~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-

    PURPOSE:  Use python subprocess module to start an instance of the
              server in the background. You can use the returned
              ServerInfo instance to interact with the server. This is
              useful for making the test self-contained.

              You can call kill_server to cleanup or restart_server
              if you want to restart during interactive testing.
    """
    s_port = s_port if s_port else str(find_free_port())
    cwd = cwd if cwd else pathlib.Path(cmd[0]).parent
    cmd.extend(['--port', s_port])
    server = run_cmd(cmd=cmd, cwd=cwd, timeout=-4, env=env)
    return ServerInfo(server, int(s_port))
