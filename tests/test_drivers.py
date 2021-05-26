import copy
import datetime
import os
import unittest

from unittest.mock import MagicMock, patch, mock_open, call

import docker.errors

import docker_pkg.drivers as drivers
from docker_pkg.cli import defaults
from docker_pkg import dockerfile, ImageLabel
from tests import fixtures_dir


class TestDockerDriver(unittest.TestCase):
    def setUp(self):
        self.docker = MagicMock()
        self.config = {}
        self.label = ImageLabel(self.config, "image_name", "image_tag")
        self.driver = drivers.DockerDriver(self.config, self.label, self.docker, True)

    def test_init(self):
        self.assertEqual(self.driver.client, self.docker)
        self.assertEqual(self.driver.label.image(), "image_name:image_tag")
        self.assertEqual(str(self.driver), "image_name:image_tag")
        self.assertTrue(self.driver.nocache)

    def test_exists(self):
        self.assertTrue(self.driver.exists())
        self.docker.images.get.assert_called_with("image_name:image_tag")
        self.driver.client.images.get.side_effect = docker.errors.ImageNotFound("test")
        self.assertFalse(self.driver.exists())

    def test_buildargs(self):
        # No proxy declared
        self.assertEqual(self.driver.buildargs, {})
        # Proxy declared
        self.driver.config["http_proxy"] = "foobar"
        self.assertEqual(self.driver.buildargs["HTTPS_PROXY"], "foobar")

    def test_prune(self):
        def mock_image(tags, id):
            image = MagicMock()
            image.attrs = {"RepoTags": tags, "Id": id}
            return image

        # The happy path works, and deletes just the right images
        self.docker.images.list.return_value = [
            mock_image(["image_name:1.0", "image_name:image_tag"], "test1"),
            mock_image(["image_name:0.9"], "test2"),
        ]
        self.assertTrue(self.driver.prune())
        self.docker.images.list.assert_called_with("image_name")
        self.docker.images.remove.assert_has_calls([call("test2")])
        self.docker.images.remove.side_effect = ValueError("error")
        self.assertFalse(self.driver.prune())

    def test_clean(self):
        self.driver.clean()
        self.driver.client.images.remove.assert_called_with("image_name:image_tag")
        # If the image is not found, execution will work anyways
        self.driver.client.images.remove.side_effect = docker.errors.ImageNotFound("test")
        self.driver.clean()

    def test_build(self):
        self.driver.config["foo"] = "bar"
        self.driver.do_build("/tmp", filename="test")
        self.docker.api.build.assert_called_with(
            path="/tmp",
            dockerfile="test",
            tag="image_name:image_tag",
            decode=True,
            nocache=True,
            rm=True,
            pull=False,
            buildargs={},
        )
        # Check that nocache is correctly passed down
        self.driver.nocache = False
        self.driver.do_build("/tmp", filename="test")
        self.docker.api.build.assert_called_with(
            path="/tmp",
            dockerfile="test",
            tag="image_name:image_tag",
            nocache=False,
            rm=True,
            pull=False,
            buildargs={},
            decode=True,
        )

        # If the build returns an error, a docker.errors.BuildError exception is raised
        self.docker.api.build.return_value = [
            {"error": "test", "errorDetail": {"message": "test! ", "code": 1}}
        ]
        with self.assertRaises(docker.errors.BuildError):
            self.driver.do_build("/tmp", filename="test")

    def test_publish_no_credentials(self):
        """Publishing without credentials raises an Exception"""
        with self.assertRaises(ValueError):
            self.driver.publish(["lol"])

    def test_publish(self):
        """Publishing works as expected"""
        self.driver.config["username"] = "u"
        self.driver.config["password"] = "p"
        self.driver.client.api.push = MagicMock()
        self.assertTrue(self.driver.publish(["sometag"]))
        self.driver.client.api.push.assert_called_with(
            "image_name", "sometag", auth_config={"username": "u", "password": "p"}
        )
