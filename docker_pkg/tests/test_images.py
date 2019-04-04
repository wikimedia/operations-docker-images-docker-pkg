import copy
import datetime
import os
import unittest

from unittest.mock import MagicMock, patch, mock_open, call

import docker.errors

import docker_pkg.image as image
from docker_pkg.cli import defaults
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
        self.assertTrue(img.nocache)

    def test_name(self):
        self.image.config['namespace'] = 'acme'
        self.assertEqual(self.image.name, 'acme/image_name')
        self.image.config['registry'] = 'example.org'
        self.assertEqual(self.image.name, 'example.org/acme/image_name')
        self.image.config['namespace'] = None
        self.assertEqual(self.image.name, 'example.org/image_name')

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

    def test_prune(self):
        def mock_image(tags, id):
            image = MagicMock()
            image.attrs = {'RepoTags': tags, 'Id': id}
            return image

        # The happy path works, and deletes just the right images
        self.docker.images.list.return_value = [
            mock_image(['image_name:1.0', 'image_name:image_tag'], 'test1'),
            mock_image(['image_name:0.9'], 'test2'),
        ]
        self.assertTrue(self.image.prune())
        self.docker.images.list.assert_called_with('image_name')
        self.docker.images.remove.assert_has_calls([call('test2')])
        self.docker.images.remove.side_effect = ValueError('error')
        self.assertFalse(self.image.prune())

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
        self.docker.api.build.assert_called_with(
            path='/tmp',
            dockerfile='test',
            tag='image_name:image_tag',
            decode=True,
            nocache=True,
            rm=True,
            pull=False,
            buildargs={}
        )
        # Check that nocache is correctly passed down
        self.image.nocache = False
        with patch('docker_pkg.image.open', m, create=True):
            self.image.build('/tmp', filename='test')
        self.docker.api.build.assert_called_with(
            path='/tmp',
            dockerfile='test',
            tag='image_name:image_tag',
            nocache=False,
            rm=True,
            pull=False,
            buildargs={},
            decode=True
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
        image.DockerImage.is_nightly = False

    def test_init(self):
        self.assertEqual(self.image.tag, '0.0.1')
        self.assertEqual(self.image.name, 'foo-bar')
        self.assertEqual(self.image.path, self.basedir)
        self.assertIsNone(self.image.build_image)
        self.assertEqual(self.image.depends, [])
        image.DockerImage.is_nightly = True
        img = image.DockerImage(self.basedir, self.docker, self.config)
        date = datetime.datetime.now().strftime(img.NIGHTLY_BUILD_FORMAT)
        self.assertEqual(img.tag, '0.0.1-{}'.format(date))
        image.DockerImage.is_nightly = False

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

    def test_safe_name(self):
        self.image.short_name = 'team-foo/test-app'
        self.assertEqual(self.image.safe_name, 'team-foo-test-app')

    @patch('os.path.isfile')
    def test_dockerignore(self, isfile):
        isfile.return_value = False
        self.assertIsNone(self.image._dockerignore())
        isfile.return_value = True
        m = mock_open(read_data='# ignore me')
        with patch('docker_pkg.image.open', m, create=True):
            self.assertIsNone(self.image._dockerignore())
        m = mock_open(read_data='# ignore me \na*\n\n \n')
        with patch('docker_pkg.image.open', m, create=True):
            with patch('docker_pkg.image.glob.glob') as mocker:
                mocker.return_value = [os.path.join(self.image.path, 'abc')]
                _filter = self.image._dockerignore()
                self.assertIsNotNone(_filter)
                mocker.assert_called_with(os.path.join(self.image.path, 'a*'))
                self.assertEqual(['abc'], _filter(self.image.path, ['abc', 'bcdef']))


    def test_build_environment(self):
        with patch('shutil.copytree') as cp:
            with patch('tempfile.mkdtemp') as mkdir:
                mkdir.return_value = '/tmp/test'
                self.image._create_build_environment()
        self.assertEqual(self.image.build_path, '/tmp/test/context')
        cp.assert_called_with(self.image.path, self.image.build_path, ignore=None)
        mkdir.assert_called_with(prefix='docker-pkg-foo-bar')
        # Test that a second call will not call any mock
        with patch('shutil.copytree') as cp:
            with patch('tempfile.mkdtemp') as mkdir:
                mkdir.return_value = '/tmp/test'
                self.image._create_build_environment()
        cp.assert_not_called()
        mkdir.asset_not_called()
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
        parent.side_effect = docker.errors.BuildError('foo!', None)
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
        bi.build.side_effect = docker.errors.BuildError('foo-build', None)
        self.assertFalse(self.image._build_artifacts())
        bi.extract.assert_not_called()
        bi.clean.assert_called_with()
        bi.clean.reset_mock()
        bi.build.side_effect = None
        bi.extract.side_effect = ValueError('test')
        self.assertFalse(self.image._build_artifacts())
        bi.clean.assert_called_with()

    def test_new_tag(self):
        # First test, check a native tag
        self.image.metadata['tag'] = '0.1.2'
        self.assertEqual(self.image.new_tag(), '0.1.2-s1')
        # Now an image with several security changes
        self.image.metadata['tag'] = '0.1.2-1-s8'
        self.assertEqual(self.image.new_tag(), '0.1.2-1-s9')
        # With a different separator
        self.image.metadata['tag'] = '0.1.2-1'
        self.assertEqual(self.image.new_tag(identifier=''), '0.1.2-2')
        # Finally an invalid version number. Please note this is valid in strict
        # debian terms but I don't consider this a particular limitation, as
        # image creators should use SemVer.
        # Moreover, tildes are not admitted in docker image tags.
        self.image.metadata['tag'] = '0.1.2-1~wmf2'
        self.assertRaises(ValueError, self.image.new_tag)

    def test_get_author(self):
        self.image.config['fallback_author'] = 'joe'
        self.image.config['fallback_email'] = 'admin@example.org'
        os.environ['DEBFULLNAME'] = 'Foo'
        os.environ['DEBEMAIL'] = 'test@example.com'
        self.assertEqual(self.image._get_author(), ('Foo', 'test@example.com'))
        # Unset debemail
        del os.environ['DEBEMAIL']
        with patch('subprocess.check_output') as co:
            co.return_value = b'other@example.com\n'
            self.assertEqual(self.image._get_author(), ('Foo', 'other@example.com'))


    def test_create_change(self):
        m = mock_open(read_data='')
        self.image.config['fallback_author'] = 'joe'
        self.image.config['fallback_email'] = 'test@example.org'
        self.image.config['distribution'] = 'pinkunicorn'
        self.image.config['update_id'] = 'L'
        with patch('docker_pkg.image.open', m, create=True) as opn:
            handle = opn.return_value
            changelog = os.path.join(self.basedir, 'changelog')
            with patch('docker_pkg.image.Changelog') as dch:
                self.image.create_update('test')
                dch.assert_called_with(handle)
                opn.assert_any_call(changelog, 'rb')
                opn.assert_any_call(changelog, 'w')
                assert dch.return_value.new_block.called
                dch.return_value.write_to_open_file.assert_called_with(handle)
