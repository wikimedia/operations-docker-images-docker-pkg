import unittest

from docker_pkg import image_fullname


class TestDocker_pkg(unittest.TestCase):

    def assertFullname(self, expected, image, cfg):
        self.assertEquals(expected, image_fullname(image, cfg))

    def test_image_fullname_with_empty_configuration(self):
        self.assertFullname('image', 'image', {})

    def test_image_fullname_with_namespace(self):
        self.assertFullname('operations/image', 'image', {'namespace': 'operations'})

    def test_image_fullname_with_registry(self):
        self.assertFullname('docker-registry.example.org/image',
                            'image', {'registry': 'docker-registry.example.org'})

    def test_image_fullname_with_registry_and_namespace(self):
        self.assertFullname(
            'docker-registry.example.org/operations/image',
            'image',
            {'namespace': 'operations', 'registry': 'docker-registry.example.org'}
        )
