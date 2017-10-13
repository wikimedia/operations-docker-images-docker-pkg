import os
import unittest

from unittest.mock import MagicMock, patch, mock_open

import docker.errors

import docker_pkg.image as image
from docker_pkg import dockerfile
from docker_pkg.tests import fixtures_dir


class AnyStringIncluding(str):
    def __eq__(self, other):
        return self in other


class TestDockerImageBase(unittest.TestCase):

    def setUp(self):
        self.docker = MagicMock()
        self.config = {}
        self.image = image.DockerImageBase(
            'image_name', 'image_tag', self.docker,
            self.config, '/home', 'abcde', '/tmp')

    def test_init(self):
        img = image.DockerImageBase('image_name', 'image_tag', self.docker, self.config,
                                     '/home', 'abcde', '/tmp')
        self.assertEqual(img.docker, self.docker)
        self.assertEqual(img.image, 'image_name:image_tag')
        self.assertEqual(img.build_path, '/tmp')
        self.assertEqual(str(img), 'image_name:image_tag')

    def test_exists(self):
        self.assertTrue(self.image.exists())
        self.docker.images.get.assert_called_with('image_name:image_tag')
        self.image.docker.images.get.side_effect = docker.errors.ImageNotFound("test")
        self.assertFalse(self.image.exists())

    def test_buildargs(self):
        # No proxy declared
        self.assertEqual(self.image.buildargs, {})
        # Proxy declared
        self.image.config['http_proxy'] = 'foobar'
        self.assertEqual(self.image.buildargs['HTTPS_PROXY'], 'foobar')

    @patch('docker_pkg.image.tarfile.open')
    @patch('docker_pkg.image.BytesIO')
    def test_extract(self, bytesmock, openmock):
        # Test docker calls in a successful extraction
        tarmock = MagicMock()
        openmock.return_value = tarmock
        self.image.extract('/build', '/tmp')
        self.image.docker.containers.create.assert_called_with(
            'image_name:image_tag', name=AnyStringIncluding('image_name-ephemeral-'),
            network_disabled=False
        )
        tarmock.extractall.assert_called_with(path='/tmp')
        tarmock.reset_mock()
        # Container creation failure does not get us to tarfile
        self.image.docker.containers.create.side_effect = docker.errors.ImageNotFound('test')
        self.assertRaises(RuntimeError, self.image.extract, '/build', '/tmp')
        tarmock.assert_not_called()
        # Tarfile extraction failure raises an exception
        self.image.docker.containers.create.side_effect = None
        tarmock.extractall.side_effect = ValueError('test!')
        with self.assertRaises(RuntimeError):
            self.image.extract('/build', '/tmp')

    def test_clean(self):
        self.image.clean()
        self.image.docker.images.remove.assert_called_with('image_name:image_tag')
        # If the image is not found, execution will work anyways
        self.image.docker.images.remove.side_effect = docker.errors.ImageNotFound('test')
        self.image.clean()

    def test_remove_container(self):
        # Test a not found container just returns False
        self.docker.containers.get.side_effect = docker.errors.NotFound('test')
        self.assertFalse(self.image.remove_container('foo'))

    def test_build(self):
        self.image.config['foo'] = 'bar'
        self.image.dockerfile_tpl = MagicMock()
        m = mock_open()
        with patch('docker_pkg.image.open', m, create=True):
            self.image.build('/tmp', filename='test')
        self.docker.images.build.assert_called_with(
            path='/tmp',
            dockerfile='test',
            tag='image_name:image_tag',
            nocache=True,
            rm=True,
            pull=False,
            buildargs={}
        )
        self.image.dockerfile_tpl.render.assert_called_with(foo='bar')
        m.assert_called_with('/tmp/test', 'w')
        # An empty dockerfile will raise an exception
        # and not open files nor call a build
        self.image.dockerfile_tpl.render.return_value = None
        self.docker.images.build.reset_mock()
        with self.assertRaises(RuntimeError):
            self.image.build('/tmp', filename='test')
        self.docker.images.build.assert_not_called()


class TestDockerImage(unittest.TestCase):

    def setUp(self):
       self.docker = MagicMock()
       self.config = {}
       dockerfile.TemplateEngine.setup(self.config, [])
       self.basedir = os.path.join(fixtures_dir, 'foo-bar')
       self.image = image.DockerImage(self.basedir, self.docker, self.config)



    def test_init(self):
        self.assertEqual(self.image.tag, '0.0.1')
        self.assertEqual(self.image.name, 'foo-bar')
        self.assertEqual(self.image.path, self.basedir)
        self.assertIsNone(self.image.build_image)
        self.assertEqual(self.image.depends, [])

    def test_init_with_build_image(self):
        base = os.path.join(fixtures_dir, 'with_build')
        with patch('docker_pkg.dockerfile.from_template') as df:
            img = image.DockerImage(base, self.docker, self.config)
        self.assertEqual(img.tag, '0.0.1~alpha1')
        self.assertEqual(img.name, 'foobar-server')
        self.assertIsInstance(img.build_image, image.DockerImageBase)
        self.assertEqual(img.build_image.tag, '0.0.1~alpha1')
        self.assertEqual(img.build_image.name, 'foobar-server-build')
        df.assert_called_with(base, 'Dockerfile.build.template')
        self.assertEqual(img.depends, ['foo-bar'])

    def test_build_environment(self):
        with patch('shutil.copytree') as cp:
            with patch('tempfile.mkdtemp') as mkdir:
                mkdir.return_value = '/tmp/test'
                self.image._create_build_environment()
        self.assertEqual(self.image.build_path, '/tmp/test/context')
        cp.assert_called_with(self.image.path, self.image.build_path)
        mkdir.assert_called_with(prefix='docker-pkg-foo-bar')
        # Now test cleaning the environment
        with patch('shutil.rmtree') as rm, patch('os.path.isdir') as isdir:
            isdir.return_value = False
            # If the dir is not present, nothing is done
            self.image._clean_build_environment()
            rm.assert_not_called()
            isdir.return_value = True
            self.image._clean_build_environment()
            rm.assert_called_with('/tmp/test')

    @patch('docker_pkg.image.DockerImageBase.build')
    def test_build_ok(self, parent):
        # Test simple image with no build artifacts
        self.image._create_build_environment = MagicMock()
        self.image._clean_build_environment = MagicMock()
        self.image.build_path = 'test'
        self.assertTrue(self.image.build())
        parent.assert_called_with('test')
        self.image._create_build_environment.assert_called_with()
        self.image._clean_build_environment.assert_called_with()

    @patch('docker_pkg.image.DockerImageBase.build')
    def test_build_bad_artifacts(self, parent):
        # Test image with failed build artifacts fails and doesn't call a build
        self.image._create_build_environment = MagicMock()
        self.image._clean_build_environment = MagicMock()
        self.image._build_artifacts = MagicMock(return_value=False)
        self.assertFalse(self.image.build())
        parent.assert_not_called()
        self.image._create_build_environment.assert_called_with()
        self.image._clean_build_environment.assert_called_with()

    @patch('docker_pkg.image.DockerImageBase.build')
    def test_build_exception(self, parent):
        # Test image that raises exception during a build is properly handled
        self.image._create_build_environment = MagicMock()
        self.image._clean_build_environment = MagicMock()
        self.image.build_path = 'test'
        parent.side_effect = docker.errors.BuildError('foo!')
        self.assertFalse(self.image.build())
        self.image._create_build_environment.assert_called_with()
        self.image._clean_build_environment.assert_called_with()
        parent.side_effect = RuntimeError('foo!')
        self.assertFalse(self.image.build())
        self.image._create_build_environment.assert_called_with()
        self.image._clean_build_environment.assert_called_with()

    def test_build_artifacts(self):
        self.image.build_path = 'test'
        bi = MagicMock(autospec=image.DockerImageBase)
        self.image.build_image = bi
        # if the build image in not present, the method will return true immediately
        self.image.build_image = None
        self.assertTrue(self.image._build_artifacts())
        bi.clean.assert_not_called()

    def test_build_artifacts_ok(self):
        self.image.build_path = 'test'
        bi = MagicMock(autospec=image.DockerImageBase)
        self.image.build_image = bi
        # A successful build
        self.assertTrue(self.image._build_artifacts())
        bi.build.assert_called_with('test', filename='Dockerfile.build')
        bi.extract.assert_called_with('/build', 'test')
        bi.clean.assert_called_with()

    def test_build_artifacts_exception(self):
        self.image.build_path = 'test'
        bi = MagicMock(autospec=image.DockerImageBase)
        self.image.build_image = bi
        # If the build fails at different times, the build is maked as failed.
        bi.build.side_effect = docker.errors.BuildError('foo-build')
        self.assertFalse(self.image._build_artifacts())
        bi.extract.assert_not_called()
        bi.clean.assert_called_with()
        bi.clean.reset_mock()
        bi.build.side_effect = None
        bi.extract.side_effect = ValueError('test')
        self.assertFalse(self.image._build_artifacts())
        bi.clean.assert_called_with()