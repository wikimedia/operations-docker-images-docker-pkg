import copy
import datetime
import os
import unittest

from unittest.mock import MagicMock, patch, mock_open, call

import docker.errors

import docker_pkg.image as image
from docker_pkg.cli import defaults
from docker_pkg import dockerfile, ImageLabel, drivers
from tests import fixtures_dir


class AnyStringIncluding(str):
    def __eq__(self, other):
        return self in other


class TestDockerImage(unittest.TestCase):
    def setUp(self):
        self.docker = MagicMock()
        self.config = {}
        dockerfile.TemplateEngine.setup(self.config, [])
        driver = drivers.get(self.config, client=self.docker, nocache=True)
        self.basedir = os.path.join(fixtures_dir, "foo-bar")
        self.image = image.DockerImage(self.basedir, driver, self.config)
        image.DockerImage.is_nightly = False

    def test_init(self):
        self.assertEqual(self.image.tag, "0.0.1")
        self.assertEqual(self.image.name, "foo-bar")
        self.assertEqual(self.image.path, self.basedir)
        self.assertEqual(self.image.depends, [])
        image.DockerImage.is_nightly = True
        img = image.DockerImage(self.basedir, self.docker, self.config)
        date = datetime.datetime.now().strftime(img.NIGHTLY_BUILD_FORMAT)
        self.assertEqual(img.tag, "0.0.1-{}".format(date))
        image.DockerImage.is_nightly = False

    def test_safe_name(self):
        self.image.label.short_name = "team-foo/test-app"
        self.assertEqual(self.image.safe_name, "team-foo-test-app")

    @patch("os.path.isfile")
    def test_dockerignore(self, isfile):
        isfile.return_value = False
        self.assertIsNone(self.image._dockerignore())
        isfile.return_value = True
        m = mock_open(read_data="# ignore me")
        with patch("docker_pkg.image.open", m, create=True):
            self.assertIsNone(self.image._dockerignore())
        m = mock_open(read_data="# ignore me \na*\n\n \n")
        with patch("docker_pkg.image.open", m, create=True):
            with patch("docker_pkg.image.glob.glob") as mocker:
                mocker.return_value = [os.path.join(self.image.path, "abc")]
                _filter = self.image._dockerignore()
                self.assertIsNotNone(_filter)
                mocker.assert_called_with(os.path.join(self.image.path, "a*"))
                self.assertEqual(["abc"], _filter(self.image.path, ["abc", "bcdef"]))

    def test_build_environment(self):
        """The build environment gets created and removed."""
        with self.image.build_environment() as be:
            self.assertTrue(os.path.isdir(be))
            self.assertTrue(os.path.isfile(os.path.join(be, "test.sh")))
        self.assertFalse(os.path.isdir(be))

    def test_build_environment_exception(self):
        """The build environment is removed even if an exception happens"""
        try:
            with self.image.build_environment() as be:
                self.assertTrue(os.path.isdir(be))
                raise ValueError(be)
        except:
            self.assertFalse(os.path.isdir(be))

    @patch("docker_pkg.drivers.DockerDriver.do_build")
    def test_build_ok(self, driver_build):
        # Test simple image with no build artifacts
        self.image.build_environment = MagicMock()
        self.image.build_environment.return_value.__enter__.return_value = "test"
        self.image.write_dockerfile = MagicMock(return_value="file_name")
        self.assertTrue(self.image.build())
        driver_build.assert_called_with("test", "file_name")

    @patch("docker_pkg.drivers.DockerDriver.do_build")
    def test_build_exception(self, builder):
        # Test image that raises exception during a build is properly handled
        builder.side_effect = docker.errors.BuildError("foo!", None)
        self.assertFalse(self.image.build())
        builder.side_effect = RuntimeError("foo!")
        self.assertFalse(self.image.build())

    def test_write_dockerfile(self):
        """Test that the dockerfile gets written"""
        with self.image.build_environment() as be:
            self.image.write_dockerfile(be)
            self.assertTrue(os.path.isfile(os.path.join(be, "Dockerfile")))

    def test_new_tag(self):
        # First test, check a native tag
        self.image.metadata["tag"] = "0.1.2"
        self.assertEqual(self.image.new_tag(), "0.1.2-s1")
        # Now an image with several security changes
        self.image.metadata["tag"] = "0.1.2-1-s8"
        self.assertEqual(self.image.new_tag(), "0.1.2-1-s9")
        # With a different separator
        self.image.metadata["tag"] = "0.1.2-1"
        self.assertEqual(self.image.new_tag(identifier=""), "0.1.2-2")
        # Finally an invalid version number. Please note this is valid in strict
        # debian terms but I don't consider this a particular limitation, as
        # image creators should use SemVer.
        # Moreover, tildes are not admitted in docker image tags.
        self.image.metadata["tag"] = "0.1.2-1~wmf2"
        self.assertRaises(ValueError, self.image.new_tag)

    def test_get_author(self):
        self.image.config["fallback_author"] = "joe"
        self.image.config["fallback_email"] = "admin@example.org"
        os.environ["DEBFULLNAME"] = "Foo"
        os.environ["DEBEMAIL"] = "test@example.com"
        self.assertEqual(self.image._get_author(), ("Foo", "test@example.com"))
        # Unset debemail
        del os.environ["DEBEMAIL"]
        with patch("subprocess.check_output") as co:
            co.return_value = b"other@example.com\n"
            self.assertEqual(self.image._get_author(), ("Foo", "other@example.com"))

    def test_create_change(self):
        m = mock_open(read_data="")
        self.image.config["fallback_author"] = "joe"
        self.image.config["fallback_email"] = "test@example.org"
        self.image.config["distribution"] = "pinkunicorn"
        self.image.config["update_id"] = "L"
        with patch("docker_pkg.image.open", m, create=True) as opn:
            handle = opn.return_value
            changelog = os.path.join(self.basedir, "changelog")
            with patch("docker_pkg.image.Changelog") as dch:
                self.image.create_update("test")
                dch.assert_called_with(handle)
                opn.assert_any_call(changelog, "rb")
                opn.assert_any_call(changelog, "w")
                assert dch.return_value.new_block.called
                dch.return_value.write_to_open_file.assert_called_with(handle)

    def test_verify_image_no_executable(self):
        self.image.config["verify_command"] = "/nonexistent"
        self.image.config["verify_args"] = []
        self.assertFalse(self.image.verify())

    def test_verify_failure(self):
        self.image.config = defaults
        self.image.config["verify_args"] = ["-c", "{path}/test.sh fail"]
        self.assertFalse(self.image.verify())

    def test_verify_success(self):
        self.image.config = defaults
        self.assertTrue(self.image.verify())

    def test_verify_no_testcase(self):
        self.image.config = defaults
        self.image.config["verify_args"] = ["-c", "{path}/test.sh.nope fail"]
        self.assertTrue(self.image.verify())
