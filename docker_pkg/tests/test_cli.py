import os
import unittest

from unittest.mock import patch, MagicMock, mock_open

import docker_pkg.cli
from docker_pkg.image import DockerImage
from docker_pkg.tests import fixtures_dir


class TestCli(unittest.TestCase):

    @patch('docker_pkg.builder.DockerBuilder')
    def test_main_build(self, builder):
        application = builder.return_value
        # Stupid test case
        args = MagicMock()
        args.configfile = os.path.join(fixtures_dir, 'config.yaml')
        args.directory = fixtures_dir
        args.nightly = False
        args.select = 'python*'
        args.nocache = True
        args.pull = True
        args.mode = 'build'
        args.debug = False
        args.info = False
        with patch('docker_pkg.cli.build') as b:
            docker_pkg.cli.main(args)
            builder.assert_called_with(fixtures_dir, docker_pkg.cli.defaults, 'python*', True, True)
            b.assert_called_with(application, False)
            args.info = True
            docker_pkg.cli.main(args)
            b.assert_called_with(application, True)
        args.snapshot = True
        with patch('docker_pkg.cli.build') as b:
            docker_pkg.cli.main(args)
            self.assertEqual(DockerImage.is_nightly, True)
            self.assertEqual(DockerImage.NIGHTLY_BUILD_FORMAT, '%Y%m%d-%H%M%S')

    @patch('docker_pkg.builder.DockerBuilder')
    def test_main_prune(self, builder):
        application = builder.return_value
        args = MagicMock()
        args.configfile = os.path.join(fixtures_dir, 'config.yaml')
        args.directory = fixtures_dir
        args.select = 'python*'
        args.mode = 'prune'
        args.info = False
        args.nightly = '20190503'
        with patch('docker_pkg.cli.prune') as p:
            docker_pkg.cli.main(args)
            p.assert_called_with(application, '20190503')
            builder.assert_called_with(fixtures_dir, docker_pkg.cli.defaults,
                                       'python*', True, False)

    @patch('docker_pkg.builder.DockerBuilder')
    def test_main_update(self, builder):
        application = builder.return_value
        args = MagicMock()
        args.configfile = os.path.join(fixtures_dir, 'config.yaml')
        args.directory = fixtures_dir
        args.select = 'python'
        args.mode = 'update'
        args.reason = '36 chambers'
        args.version = 'version!'
        with patch('docker_pkg.cli.update') as u:
            docker_pkg.cli.main(args)
            u.assert_called_with(application, '36 chambers', 'python', 'version!')
            builder.assert_called_with(fixtures_dir, docker_pkg.cli.defaults,
                                       '*python:*', True, False)

        args.mode = 'foobar'
        self.assertRaises(ValueError, docker_pkg.cli.main, args)

    def test_parse_args(self):
        test_args = [
            '-c', 'cfg', '--info', 'prune',
            '--select', '*yp*', 'someDir'
        ]

        args = docker_pkg.cli.parse_args(test_args)
        self.assertEqual(args.configfile, 'cfg')
        self.assertEqual(args.select, '*yp*')
        self.assertEqual(args.directory, 'someDir')
        self.assertEqual(args.mode, 'prune')

    def test_parse_args_build(self):
        # now a build
        test_args = [
             '-c', 'cfg', '--info', 'build', '--no-pull', '--select', '*yp*', 'someDir'
        ]
        args = docker_pkg.cli.parse_args(test_args)
        self.assertEqual(args.mode, 'build')
        self.assertFalse(args.pull)
        self.assertTrue(args.nocache)

    def test_parse_args_update(self):
        # finally an update
        test_args = [
            '-c', 'cfg', '--debug', 'update', 'someImage', 'someDir'
        ]
        args = docker_pkg.cli.parse_args(test_args)
        self.assertEqual(args.mode, 'update')
        self.assertEqual(args.select, 'someImage')
        self.assertEqual(args.directory, 'someDir')
        self.assertTrue(args.debug)

    def test_read_config(self):
        m = mock_open(read_data='registry: docker-reg.example.org')
        with patch('docker_pkg.cli.open', m, create=True):
            conf = docker_pkg.cli.read_config('/dev/null')
        self.assertIn('registry', conf)
        self.assertEqual('docker-reg.example.org', conf['registry'])

        self.assertNotEqual('docker-reg.example.org',
            docker_pkg.cli.defaults['registry'],
            'docker_pkg.cli.defaults must not be altered')
