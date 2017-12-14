import os
import unittest

from unittest.mock import patch, MagicMock, call

from docker_pkg.builder import DockerBuilder, ImageFSM
from docker_pkg import dockerfile, image
from docker_pkg.tests import fixtures_dir


class TestDockerBuilder(unittest.TestCase):
    def setUp(self):
        dockerfile.TemplateEngine.setup({}, [])
        with patch('docker.from_env'):
            self.builder = DockerBuilder(fixtures_dir, {'seed_image': 'test'})

    def img_metadata(self, name, tag, deps):
        img = ImageFSM(os.path.join(fixtures_dir, 'foo-bar'), self.builder.client, self.builder.config)
        img.image.short_name = name
        img.image.tag = tag
        img.image.metadata['depends'] = deps
        img.state = 'to_build'
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

    def test_scan(self):
        with patch('docker_pkg.image.DockerImageBase.exists') as mocker:
            mocker.return_value = False
            self.builder.scan()
        self.assertEqual(self.builder.known_images, {'foo-bar:0.0.1', 'foobar-server:0.0.1~alpha1'})
        # Build chain is correctly ordered
        bc = [img.label for img in self.builder.build_chain]
        self.assertEqual(bc, ['foo-bar:0.0.1', 'foobar-server:0.0.1~alpha1'])

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

    def test_images_in_state(self):
        img0 = ImageFSM(os.path.join(fixtures_dir, 'foo-bar'), self.builder.client, self.builder.config)
        img1 = ImageFSM(os.path.join(fixtures_dir, 'with_build'), self.builder.client, self.builder.config)
        img0.state = 'built'
        img1.state = 'error'
        self.builder.all_images = set([img0, img1])
        self.assertEqual([img0], self.builder.images_in_state('built'))

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
        self.builder.client.api.tag.assert_not_called()
        self.builder.config['username'] = 'foo'
        self.builder.config['password'] = 'bar'

        result = [r for r in self.builder.publish()]
        self.assertEqual('published', result[1].state)
        self.builder.client.api.push.assert_any_call(
            'example.org/foobar-server', '0.0.1~alpha1', auth_config={'username': 'foo', 'password': 'bar'})
