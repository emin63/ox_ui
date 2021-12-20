
import os
import pathlib
import unittest

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

    @classmethod
    def tearDownClass(cls):
        s_proc = cls.server_info.s_proc
        if s_proc:
            s_proc.kill()
            s_proc.wait(timeout=10)
            cls.server_info.s_proc = None

    def test_good_c2f(self):
        url = 'http://127.0.0.1:%s/hello' % (self.server_info.s_port)
        count, text = 3, 'bye'
        self.check_good_c2f(url, count, text)
        
        count, text = 2, 'why'
        url = 'http://127.0.0.1:%s/goodbye' % (self.server_info.s_port)
        self.check_good_c2f(
            url, count, text, when='2021-01-23',
            more='\n\nat 2021-01-23 00:00:00')

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

