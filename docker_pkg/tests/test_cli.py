import os
import unittest

from unittest.mock import patch, MagicMock, mock_open

import docker_pkg.cli
from docker_pkg.tests import fixtures_dir


class TestCli(unittest.TestCase):

    @patch('docker_pkg.builder.DockerBuilder')
    def test_main(self, builder):
        application = builder.return_value
        # Stupid test case
        args = MagicMock()
        args.configfile = os.path.join(fixtures_dir, 'config.yaml')
        args.directory = fixtures_dir
        args.nightly = False
        args.select = 'python*'
        args.nocache=True
        args.pull=True
        args.action = 'build'
        args.debug = False
        args.info = False
        with patch('docker_pkg.cli.build') as b:
            docker_pkg.cli.main(args)
            builder.assert_called_with(fixtures_dir, docker_pkg.cli.defaults, 'python*', True, True)
            b.assert_called_with(application, False)
        args.action = 'prune'
        with patch('docker_pkg.cli.prune') as p:
            docker_pkg.cli.main(args)
            p.assert_called_with(application)
        args.action = 'foobar'
        self.assertRaises(ValueError, docker_pkg.cli.main, args)


    def test_parse_args(self):
        test_args = [
            '-c', 'cfg', '--info',
            '--select', '*yp*', '--no-pull',
            'someDir', 'prune'
        ]

        args = docker_pkg.cli.parse_args(test_args)
        self.assertEqual(args.configfile, 'cfg')
        self.assertEqual(args.select, '*yp*')
        self.assertEqual(args.directory, 'someDir')
        self.assertEqual(args.action, 'prune')
        # now with the default action
        test_args.pop()
        args = docker_pkg.cli.parse_args(test_args)
        self.assertEqual(args.action, 'build')

    def test_read_config(self):
        m = mock_open(read_data='registry: docker-reg.example.org')
        with patch('docker_pkg.cli.open', m, create=True):
            conf = docker_pkg.cli.read_config('/dev/null')
        self.assertIn('registry', conf)
        self.assertEqual('docker-reg.example.org', conf['registry'])

        self.assertNotEqual('docker-reg.example.org',
            docker_pkg.cli.defaults['registry'],
            'docker_pkg.cli.defaults must not be altered')
