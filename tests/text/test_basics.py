"""Test some basic things.
"""

import datetime
import unittest
import click

from ox_ui.core import c2g


@click.command()
@click.option('--name', '-n', default='you',
              help='Name of person to hello.')
@click.option('--count', '-c', help='How many times to repeat.',
              default=1)
@click.option('--when', '-w', type=click.DateTime())
def hello_world(name, count, when):
    "Say hello to person multiple times."

    if when is None:
        result = []
    else:
        result = ['At ' + str(when.date()) + ': ']
    result.extend(['Hello ' + name]*count)
    return result



class TestHelloWorld(unittest.TestCase):

    def test_when(self):
        hw_cmd = c2g.ClickToGeneric(hello_world)
        result = hw_cmd.handle_request({
            'when': datetime.datetime(2021, 2, 3, 4, 5, 6)})
        self.assertEqual(result, ['At 2021-02-03: ', 'Hello you'])
        
    def test_default(self):
        hw_cmd = c2g.ClickToGeneric(hello_world)
        result = hw_cmd.handle_request()
        self.assertEqual(result, ['Hello you'])        
        
    def test_notyou(self):
        hw_cmd = c2g.ClickToGeneric(hello_world)
        result = hw_cmd.handle_request({'name': 'notyou'})
        self.assertEqual(result, ['Hello notyou'])
        
    def test_you_2(self):
        hw_cmd = c2g.ClickToGeneric(hello_world)
        result = hw_cmd.handle_request({'count': 2})
        self.assertEqual(len(result), 2)


if __name__ == '__main__':
    print('Runnig tests')
    unittest.main()
