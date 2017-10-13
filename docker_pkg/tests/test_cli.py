import os
import unittest

from unittest.mock import patch, MagicMock

import docker_pkg.cli
from docker_pkg.tests import fixtures_dir


class TestCli(unittest.TestCase):

    @patch('docker_pkg.builder.DockerBuilder')
    def test_main(self, builder):
        # Stupid test case
        args = MagicMock()
        args.configfile = os.path.join(fixtures_dir, 'config.yaml')
        args.directory = fixtures_dir
        docker_pkg.cli.main(args)
        builder.assert_called_with(fixtures_dir, docker_pkg.cli.defaults)
