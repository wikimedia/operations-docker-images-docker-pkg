import logging
import os
import unittest

from unittest.mock import patch, MagicMock, call

from docker_pkg.builder import DockerBuilder, ImageFSM
from docker_pkg import dockerfile, image
from docker_pkg.tests import fixtures_dir


class TestImageFSM(unittest.TestCase):
    def setUp(self):
        dockerfile.TemplateEngine.setup({}, [])

    @patch('docker.from_env')
    def test_init(self, client):
        img = ImageFSM(
            os.path.join(fixtures_dir, 'foo-bar'),
            client,
            {'seed_image': 'test'}
        )
        self.assertIsInstance(img.image, image.DockerImage)
        self.assertEqual(img.children, set())
        # Reinitializing the same image raises an error
        self.assertRaises(RuntimeError, ImageFSM,
                          os.path.join(fixtures_dir, 'foo-bar'),
                          client,  {'seed_image': 'test'})

class TestDockerBuilder(unittest.TestCase):
    def setUp(self):
        dockerfile.TemplateEngine.setup({}, [])
        with patch('docker.from_env'):
            self.builder = DockerBuilder(fixtures_dir, {'seed_image': 'test'})
        ImageFSM._instances = []

    def img_metadata(self, name, tag, deps):
        img = ImageFSM(os.path.join(fixtures_dir, 'foo-bar'), self.builder.client, self.builder.config)
        img.image.short_name = name
        # Clean up the images registry before initiating images this way.
        ImageFSM._instances.pop()
        ImageFSM._instances.append(name)
        img.image.tag = tag
        img.image.metadata['depends'] = deps
        img.state = ImageFSM.STATE_TO_BUILD
        return img

    @patch('docker.from_env')
    def test_init(self, client):
        # Absolute paths are untouched
        db = DockerBuilder('/test', {})
        self.assertEqual(db.root, '/test')
        # Relative paths are treated appropriately
        db = DockerBuilder('test', {})
        self.assertEqual(db.root, os.path.join(os.getcwd(), 'test'))
        # If base_images are given, they are correctly imported
        db = DockerBuilder('test', {'base_images': ['foo:0.0.1', 'bar:1.0.0']})
        self.assertEqual(db.known_images, {'foo:0.0.1', 'bar:1.0.0'})
        self.assertIsNone(db.glob)


    def test_scan(self):
        with patch('docker_pkg.image.DockerImageBase.exists') as mocker:
            mocker.return_value = False
            self.builder.scan()
        self.assertEqual(self.builder.known_images, {'foo-bar:0.0.1', 'foobar-server:0.0.1~alpha1'})
        # Build chain is correctly ordered
        bc = [img.label for img in self.builder.build_chain]
        self.assertEqual(bc, ['foo-bar:0.0.1', 'foobar-server:0.0.1~alpha1'])

    def test_scan_skips_when_missing_changelog(self):
        with patch('os.walk') as os_walk:
            os_walk.return_value = [
                ('image_with_template', [], ['Dockerfile.template'])]
            with self.assertLogs(level='WARNING') as logger:
                self.builder.scan()
                self.assertEqual(
                    logger.output,
                    ['WARNING:docker_pkg:Ignoring image_with_template since it lacks a changelog'])

    def test_scan_skips_when_missing_dockerfile_template(self):
        with patch('os.walk') as os_walk:
            os_walk.return_value = [
                ('image_with_changelog', [], ['changelog'])]
            with self.assertLogs(level='WARNING') as logger:
                self.builder.scan()
                self.assertEqual(
                    logger.output,
                    ['WARNING:docker_pkg:Ignoring image_with_changelog since it lacks a Dockerfile.template'])

    def test_scan_silently_skips_when_missing_dockerfile_template_and_changelog(self):
        with patch('os.walk') as os_walk:
            os_walk.return_value = [('image_with_no_files', [], [])]
            with self.assertLogs() as logger:
                self.builder.scan()
                # assertLogs() requires at least one message
                logging.getLogger('dummy').info('fakemessage')
                self.assertEqual(logger.output, ['INFO:dummy:fakemessage'])

    def test_build_chain(self):
        # Simple test for a linear dependency tree
        a = self.img_metadata('a', '1.0', [])
        b = self.img_metadata('b', '1.0', ['a'])
        c = self.img_metadata('c', '1.0', ['b'])
        d = self.img_metadata('d', '1.0', ['a', 'c'])
        self.builder.all_images = set([a, b, c, d])
        self.assertListEqual(self.builder.build_chain, [a, b, c, d])
        # throw an unrelated thing in the mix
        e = self.img_metadata('e', '1.0', [])
        self.builder.all_images.add(e)
        bc = self.builder.build_chain
        pos_a = bc.index(a)
        pos_b = bc.index(b)
        pos_c = bc.index(c)
        pos_d = bc.index(d)
        assert pos_a < pos_b
        assert pos_b < pos_c
        assert pos_c < pos_d
        # if the glob is present, other images will not be built
        self.builder.glob = 'e*'
        bc = self.builder.build_chain
        self.assertEqual(bc, [e])
        self.builder.glob = 'c*'
        self.assertEqual(self.builder.build_chain, [a, b, c])
        self.builder.glob = None
        # Missing dependency doesn't raise an exception (can be an external one)
        self.builder.all_images.remove(c)
        self.builder.all_images.remove(e)
        bc = self.builder.build_chain
        pos_a = bc.index(a)
        pos_b = bc.index(b)
        pos_d = bc.index(d)
        assert pos_a < pos_b
        assert pos_a < pos_d
        # Circular dependency raises an exception
        self.builder.all_images.add(self.img_metadata('c', '1.0', ['d']))
        with self.assertRaises(RuntimeError):
            self.builder.build_chain

    def test_build_dependencies(self):
        # Simple test for a linear dependency tree
        a = self.img_metadata('a', '1.0', [])
        b = self.img_metadata('b', '1.0', ['a'])
        c = self.img_metadata('c', '1.0', ['b'])
        d = self.img_metadata('d', '1.0', ['a', 'c', 'f'])
        f = self.img_metadata('f', '1.0', [])
        self.builder.all_images = set([a, b, c, d, f])
        self.builder._build_dependencies()
        assert a.children == {b, d}
        assert b.children == {c}
        assert c.children == {d}
        assert d.children == set()
        assert f.children == {d}
        # Now add a non-existing dependency
        f.image.metadata['depends'] = ['unicorn']
        self.assertRaisesRegex(
            RuntimeError,
            r'Image unicorn .* not found',
            self.builder._build_dependencies
        )

    def test_images_to_update(self):
        a = self.img_metadata('a', '1.0', [])
        b = self.img_metadata('b', '1.0', ['a'])
        c = self.img_metadata('c', '1.0', ['b'])
        d = self.img_metadata('d', '1.0', ['a', 'c', 'f'])
        f = self.img_metadata('f', '1.0', [])
        self.builder.all_images = set([a, b, c, d, f])
        self.builder.glob = '*c:*'
        assert self.builder.images_to_update() == {c, d}
        self.builder.glob = '*a:*'
        assert self.builder.images_to_update() == {a, b, c, d}

    @patch('docker_pkg.image.DockerImageBase.exists')
    @patch('docker_pkg.image.DockerImage.build')
    def test_build(self, build, exists):
        # Simple build
        exists.return_value = False
        build.side_effect = [True, False]
        img0 = ImageFSM(os.path.join(fixtures_dir, 'foo-bar'), self.builder.client, self.builder.config)
        img1 = ImageFSM(os.path.join(fixtures_dir, 'with_build'), self.builder.client, self.builder.config)
        self.builder.all_images = set([img0, img1])
        result = [r for r in self.builder.build()]
        self.assertEqual('foo-bar:0.0.1', result[0].label)
        self.assertEqual('built', result[0].state)
        self.builder.client.api.tag.assert_any_call('foo-bar:0.0.1', 'foo-bar', 'latest')
        self.assertEqual('foobar-server:0.0.1~alpha1', result[1].label)
        self.assertEqual('error', result[1].state)

    @patch('docker_pkg.image.DockerImage.build')
    @patch('docker_pkg.builder.DockerBuilder.pull_dependencies')
    def test_build_pull(self, pull, build):
        self.builder.pull = True

        def pull_result(img):
            if img.label == 'foobar-server:0.0.1~alpha1':
                img.state = ImageFSM.STATE_ERROR

        pull.side_effect = pull_result
        img0 = ImageFSM(os.path.join(fixtures_dir, 'foo-bar'), self.builder.client, self.builder.config)
        img1 = ImageFSM(os.path.join(fixtures_dir, 'with_build'), self.builder.client, self.builder.config)
        img0.state = ImageFSM.STATE_TO_BUILD
        img1.state = ImageFSM.STATE_TO_BUILD
        build.return_value = True
        self.builder.all_images = set([img0, img1])
        result = [r for r in self.builder.build()]
        pull.assert_has_calls([call(img0), call(img1)])
        assert build.call_count == 1

    def test_pull_images(self):
        img0 = ImageFSM(os.path.join(fixtures_dir, 'foo-bar'), self.builder.client, self.builder.config)
        img1 = ImageFSM(os.path.join(fixtures_dir, 'with_build'), self.builder.client, self.builder.config)
        img0.state = ImageFSM.STATE_BUILT
        img1.state = ImageFSM.STATE_TO_BUILD
        self.builder.all_images = set([img0, img1])
        # img1 is locally built, but not published. We should not pull anything
        self.builder.pull_dependencies(img1)
        assert self.builder.client.images.pull.call_count == 0
        # now if it's published, we should pull it instead
        img0.state = ImageFSM.STATE_PUBLISHED
        self.builder.pull_dependencies(img1)
        self.builder.client.images.pull.assert_called_with(img0.image.name)


    def test_images_in_state(self):
        img0 = ImageFSM(os.path.join(fixtures_dir, 'foo-bar'), self.builder.client, self.builder.config)
        img1 = ImageFSM(os.path.join(fixtures_dir, 'with_build'), self.builder.client, self.builder.config)
        img0.state = ImageFSM.STATE_BUILT
        img1.state = ImageFSM.STATE_ERROR
        self.builder.all_images = set([img0, img1])
        self.assertEqual([img0], self.builder.images_in_state(ImageFSM.STATE_BUILT))

    def test_publish(self):
        self.builder.client.api = MagicMock()
        self.builder.config['username'] = None
        self.builder.config['password'] = None
        self.builder.config['registry'] = 'example.org'
        with patch('docker_pkg.builder.ImageFSM._is_published') as mp:
            mp.return_value = False
            img0 = ImageFSM(os.path.join(fixtures_dir, 'foo-bar'), self.builder.client, self.builder.config)
            img1 = ImageFSM(os.path.join(fixtures_dir, 'with_build'), self.builder.client, self.builder.config)
        img0.state = 'built'
        img1.state = 'built'
        self.builder.all_images = set([img0, img1])
        self.assertEqual([], [r for r in self.builder.publish()])
        assert self.builder.client.api.tag.call_count == 0
        self.builder.config['username'] = 'foo'
        self.builder.config['password'] = 'bar'

        result = [r for r in self.builder.publish()]
        self.assertEqual(ImageFSM.STATE_PUBLISHED, result[1].state)
        self.builder.client.api.push.assert_any_call(
            'example.org/foobar-server', '0.0.1~alpha1', auth_config={'username': 'foo', 'password': 'bar'})
