import unittest

from docker_pkg import ImageLabel


class TestDocker_pkg(unittest.TestCase):
    def assertLabels(self, expected, config, name, version):
        label = ImageLabel(config, name, version)
        for k, expected in expected.items():
            self.assertEqual(label.label(k), expected)

    def test_image_fullname_with_empty_configuration(self):
        expected = {"short": "image", "name": "image", "full": "image:version"}
        self.assertLabels(expected, {}, "image", "version")

    def test_image_fullname_with_namespace(self):
        expected = {
            "short": "image",
            "name": "operations/image",
            "full": "operations/image:version",
        }
        self.assertLabels(expected, {"namespace": "operations"}, "image", "version")

    def test_image_fullname_with_registry(self):
        expected = {
            "short": "image",
            "name": "docker-registry.example.org/image",
            "full": "docker-registry.example.org/image:version",
        }
        self.assertLabels(expected, {"registry": "docker-registry.example.org"}, "image", "version")

    def test_image_fullname_with_registry_and_namespace(self):
        expected = {
            "short": "image",
            "name": "docker-registry.example.org/operations/image",
            "full": "docker-registry.example.org/operations/image:version",
        }
        self.assertLabels(
            expected,
            {"namespace": "operations", "registry": "docker-registry.example.org"},
            "image",
            "version",
        )
