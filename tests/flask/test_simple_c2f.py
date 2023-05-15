"""Test using flask server.
"""

import logging
import os
import socket
import time
import pathlib
import unittest
import tempfile

import requests

import ox_ui
from ox_ui.test_tools import server_tools


class BasicC2FTest(unittest.TestCase):

    server_info = None

    @classmethod
    def setUpClass(cls):
        cmd = ['flask', 'run']
        cwd = pathlib.Path(ox_ui.__file__).parents[1].joinpath(
            'tests', 'flask')
        env = os.environ.copy()
        env['FLASK_APP'] = str(cwd.joinpath('c2f_app.py'))
        cls.server_info = server_tools.start_server(cmd=cmd, cwd=cwd, env=env)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        for attempt in range(10):
            result = sock.connect_ex(('127.0.0.1', cls.server_info.s_port))
            if result == 0:
                logging.info('Succesfully started server at port %s',
                             cls.server_info.s_port)
                break
            else:
                logging.warning('Sleeping 2**%i to wait for server', attempt)
                time.sleep(2**i)
        else:
            raise ValueError('Could not start flask server.')

    @classmethod
    def tearDownClass(cls):
        s_proc = cls.server_info.s_proc
        if s_proc:
            s_proc.kill()
            s_proc.wait(timeout=10)
            cls.server_info.s_proc = None

    def test_count_file_size(self):
        url = 'http://127.0.0.1:%s/count_file_size' % (self.server_info.s_port)
        with tempfile.TemporaryDirectory() as tmpdir:
            datafile = tmpdir + '/data.txt'
            data = 'example\n'
            with open(datafile, 'w') as fdesc:
                fdesc.write(data)
            with open(datafile, 'r') as fdesc:
                req = requests.post(url, files=[('datafile', fdesc)])
                self.assertEqual(req.status_code, 200)
                self.assertEqual(req.text, str(len(data)))

    def test_choice(self):
        "Test if the choice mapping works right."

        url = 'http://127.0.0.1:%s/favorite_color' % (
            self.server_info.s_port)
        post_req = requests.post(url, data={'color': 'green'})  # default
        self.assertEqual(post_req.status_code, 200)
        self.assertEqual(post_req.text, 'I like green as well.')
        post_req = requests.post(url, data={'color': 'red'})  # choose red
        self.assertEqual(post_req.status_code, 200)
        self.assertEqual(post_req.text, 'I like red as well.')

    def test_good_c2f(self):
        url = 'http://127.0.0.1:%s/hello' % (self.server_info.s_port)
        count, text = 3, 'bye'
        self.check_good_c2f(url, count, text)

        count, text = 2, 'why'
        url = 'http://127.0.0.1:%s/goodbye' % (self.server_info.s_port)
        self.check_good_c2f(
            url, count, text, when='2021-01-23', also='blah',
            more='\nblah\n\nat 2021-01-23 00:00:00')

    def check_good_c2f(self, url, count, text, more='', **extras):

        data = {'count': count, 'text': text}
        data.update(extras)
        get_req = requests.get(url)
        self.assertEqual(get_req.status_code, 200)
        post_req = requests.post(url, data=data)
        self.assertEqual(post_req.status_code, 200)
        self.assertEqual(post_req.text, '\n'.join([text]*count) + more)

    def test_bad_c2f(self):
        url = 'http://127.0.0.1:%s/hello' % (self.server_info.s_port)
        get_req = requests.get(url)
        self.assertEqual(get_req.status_code, 200)
        count, text = 'bad', 'bye'
        post_req = requests.post(url, data={'count': count, 'text': text})
        self.assertEqual(post_req.status_code, 200)
