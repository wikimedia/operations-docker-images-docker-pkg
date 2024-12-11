import copy
import logging
import os
from pathlib import Path
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, call, patch

from docker_pkg import dockerfile, drivers, image
from docker_pkg.builder import DockerBuilder, ImageFSM

from tests import fixtures_dir


class TestImageFSM(unittest.TestCase):
    default_configuration = {"base_images": ["test:123"]}

    def setUp(self):
        ImageFSM._instances = []
        dockerfile.TemplateEngine.setup({}, [])
        with patch("docker.from_env") as client:
            self.img = ImageFSM(
                os.path.join(fixtures_dir, "foo-bar"),
                client,
                copy.deepcopy(self.default_configuration),
            )

    def test_init(self):
        self.assertIsInstance(self.img.image, image.DockerImage)
        self.assertEqual(self.img.children, set())
        # Reinitializing the same image raises an error
        self.assertRaises(
            RuntimeError,
            ImageFSM,
            os.path.join(fixtures_dir, "foo-bar"),
            MagicMock(),  # here should go a docker client
            self.default_configuration,
        )

    @patch("docker.from_env")
    @patch("docker_pkg.image.DockerImage.exists")
    def test_image_state(self, exists, client):
        exists.return_value = True
        ImageFSM._instances = []
        # We set up no registry, thus we can't have a published image.
        img = ImageFSM(os.path.join(fixtures_dir, "foo-bar"), client, self.default_configuration)
        self.assertEqual(img.state, ImageFSM.STATE_BUILT)
        ImageFSM._instances = []
        exists.return_value = False
        img = ImageFSM(os.path.join(fixtures_dir, "foo-bar"), client, self.default_configuration)
        self.assertEqual(img.state, ImageFSM.STATE_TO_BUILD)

    def test_label(self):
        self.assertEqual(self.img.label, "foo-bar:0.0.1")
        self.img.image.label.namespace = "test"
        self.img.image.label.registry = "example.org"
        self.assertEqual(self.img.label, "example.org/test/foo-bar:0.0.1")

    def test_name(self):
        self.assertEqual(self.img.name, "foo-bar")

    def test_repr(self):
        self.assertEqual(repr(self.img), "ImageFSM(foo-bar:0.0.1, built)")

    @patch("docker_pkg.image.DockerImage.build")
    def test_build(self, build):
        # An already built image doesn't get built again
        self.img.state = ImageFSM.STATE_BUILT
        self.img.build()
        self.assertEqual(build.call_count, 0)
        # Trying to build a published image raises an error
        self.img.state = ImageFSM.STATE_PUBLISHED
        self.assertRaises(ValueError, self.img.build)
        # If the build fails, we end up in an error state
        self.img.state = ImageFSM.STATE_TO_BUILD
        build.return_value = False
        self.img.build()
        self.assertEqual(self.img.state, ImageFSM.STATE_ERROR)
        # Happy path: to build => built
        self.img.state = ImageFSM.STATE_TO_BUILD
        build.return_value = True
        self.img.build()
        self.assertEqual(self.img.state, ImageFSM.STATE_BUILT)

    @patch("docker_pkg.image.DockerImage.verify")
    def test_verify(self, verify):
        self.img.state = ImageFSM.STATE_BUILT
        self.img.verify()
        self.assertEqual(self.img.state, ImageFSM.STATE_VERIFIED)
        self.assertEqual(verify.call_count, 1)

    @patch("docker_pkg.image.DockerImage.verify")
    def test_verify_no_action(self, verify):
        self.img.state = ImageFSM.STATE_VERIFIED
        self.img.verify()
        self.assertEqual(verify.call_count, 0)

    @patch("docker_pkg.image.DockerImage.verify")
    def test_verify_bad_state(self, verify):
        self.img.state = ImageFSM.STATE_TO_BUILD
        self.assertRaises(ValueError, self.img.verify)
        self.assertEqual(verify.call_count, 0)


class TestDockerBuilder(unittest.TestCase):
    default_configuration = {"base_images": ["test"]}

    def setUp(self):
        dockerfile.TemplateEngine.setup({}, [])
        with patch("docker.from_env"):
            self.builder = DockerBuilder(fixtures_dir, copy.deepcopy(self.default_configuration))
        ImageFSM._instances = []

    def img_metadata(self, name, tag, deps):
        img = ImageFSM(
            os.path.join(fixtures_dir, "foo-bar"), self.builder.client, self.builder.config
        )
        img.image.label.short_name = name
        # Clean up the images registry before initiating images this way.
        ImageFSM._instances.pop()
        ImageFSM._instances.append(name)
        img.image.label.version = tag
        img.image.metadata["depends"] = deps
        img.state = ImageFSM.STATE_TO_BUILD
        return img

    @patch("docker.from_env")
    def test_init(self, client):
        # Absolute paths are untouched
        db = DockerBuilder("/test", {})
        self.assertEqual(db.root, "/test")
        # Relative paths are treated appropriately
        db = DockerBuilder("test", {})
        self.assertEqual(db.root, os.path.join(os.getcwd(), "test"))
        # If base_images are given, they are correctly imported
        db = DockerBuilder("test", {"base_images": ["foo:0.0.1", "bar:1.0.0"]})
        self.assertEqual(db.known_images, {"foo:0.0.1", "bar:1.0.0"})
        self.assertIsNone(db.glob)

    def test_scan(self):
        self.assertEqual(self.builder.known_images, {"test"})
        with patch("docker_pkg.drivers.DockerDriver.exists") as mocker:
            mocker.return_value = False
            self.builder.scan()
        self.assertEqual(
            self.builder.known_images, {
                "test", "foo-bar:0.0.1", "foobar-server:0.0.1~alpha1",
                "upstream-version:1.63.0-1",
                "upstream-version-extended:1.63.0-1-20241211"
            }
        )
        # Build chain is complete, and correctly ordered
        bc = [img.label for img in self.builder.build_chain]
        self.assertCountEqual(bc, {
            "foo-bar:0.0.1", "foobar-server:0.0.1~alpha1",
            "upstream-version:1.63.0-1",
            "upstream-version-extended:1.63.0-1-20241211"
        })
        self.assertLess(bc.index("foo-bar:0.0.1"), bc.index("foobar-server:0.0.1~alpha1"))

    def test_scan_skips_when_missing_changelog(self):
        with patch("os.walk") as os_walk:
            os_walk.return_value = [("image_with_template", [], ["Dockerfile.template"])]
            with self.assertLogs(level="WARNING") as logger:
                self.builder.scan()
                self.assertEqual(
                    logger.output,
                    ["WARNING:docker_pkg:Ignoring image_with_template since it lacks a changelog"],
                )

    def test_scan_skips_when_missing_dockerfile_template(self):
        with patch("os.walk") as os_walk:
            os_walk.return_value = [("image_with_changelog", [], ["changelog"])]
            with self.assertLogs(level="WARNING") as logger:
                self.builder.scan()
                self.assertEqual(
                    logger.output,
                    [
                        "WARNING:docker_pkg:Ignoring image_with_changelog since it lacks a Dockerfile.template"
                    ],
                )

    def test_scan_silently_skips_when_missing_dockerfile_template_and_changelog(self):
        with patch("os.walk") as os_walk:
            os_walk.return_value = [("image_with_no_files", [], [])]
            with self.assertLogs() as logger:
                self.builder.scan()
                # assertLogs() requires at least one message
                logging.getLogger("dummy").info("fakemessage")
                self.assertEqual(logger.output, ["INFO:dummy:fakemessage"])

    def test_scan_raises_if_duplicate(self):
        with patch("os.walk") as os_walk:
            os_walk.return_value = [
                (
                    os.path.join(fixtures_dir, "foo-bar"),
                    [],
                    ["changelog", "control", "Dockerfile.template"],
                ),
                (
                    os.path.join(fixtures_dir, "foo-bar"),
                    [],
                    ["changelog", "control", "Dockerfile.template"],
                ),
            ]
            with self.assertRaises(RuntimeError):
                self.builder.scan(max_workers=4)

    def test_build_chain(self):
        # Simple test for a linear dependency tree
        a = self.img_metadata("a", "1.0", [])
        b = self.img_metadata("b", "1.0", ["a"])
        c = self.img_metadata("c", "1.0", ["b"])
        d = self.img_metadata("d", "1.0", ["a", "c"])
        self.builder.all_images = set([a, b, c, d])
        self.assertListEqual(self.builder.build_chain, [a, b, c, d])
        self.assertListEqual(self.builder.prune_chain(), [d, c, b, a])
        # throw an unrelated thing in the mix
        e = self.img_metadata("e", "1.0", [])
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
        self.builder.glob = "e*"
        bc = self.builder.build_chain
        self.assertEqual(bc, [e])
        self.builder.glob = "c*"
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
        self.builder.all_images.add(self.img_metadata("c", "1.0", ["d"]))
        with self.assertRaises(RuntimeError):
            self.builder.build_chain

    def test_prune_chain(self):
        """Test that the prune chain behaves as expected."""
        # Simple test for a linear dependency tree
        a = self.img_metadata("a1", "1.0", [])
        b = self.img_metadata("b1", "1.0", ["a1"])
        c = self.img_metadata("c2", "1.0", ["b1"])
        d = self.img_metadata("d2", "1.0", ["a1", "c2"])
        self.builder.all_images = set([a, b, c, d])
        pc = self.builder.prune_chain()
        self.assertListEqual(self.builder.prune_chain(), [d, c, b, a])
        # verify they're all set as TO_BUILD
        for fsm in pc:
            self.assertEqual(fsm.state, ImageFSM.STATE_TO_BUILD)
        # verify a glob correctly selects only the selected images
        self.builder.glob = "*2:*"
        pc = self.builder.prune_chain()
        self.assertEqual(pc, [d, c])

    def test_build_dependencies(self):
        # Simple test for a linear dependency tree
        a = self.img_metadata("a", "1.0", [])
        b = self.img_metadata("b", "1.0", ["a"])
        c = self.img_metadata("c", "1.0", ["b"])
        d = self.img_metadata("d", "1.0", ["a", "c", "f"])
        f = self.img_metadata("f", "1.0", [])
        self.builder.all_images = set([a, b, c, d, f])
        self.builder._build_dependencies()
        assert a.children == {b, d}
        assert b.children == {c}
        assert c.children == {d}
        assert d.children == set()
        assert f.children == {d}
        # Now add a non-existing dependency
        f.image.metadata["depends"] = ["unicorn"]
        self.assertRaisesRegex(
            RuntimeError, r"Image unicorn .* not found", self.builder._build_dependencies
        )

    def test_images_to_update(self):
        a = self.img_metadata("a", "1.0", [])
        b = self.img_metadata("b", "1.0", ["a"])
        c = self.img_metadata("c", "1.0", ["b"])
        d = self.img_metadata("d", "1.0", ["a", "c", "f"])
        f = self.img_metadata("f", "1.0", [])
        self.builder.all_images = set([a, b, c, d, f])
        self.builder.glob = "*c:*"
        assert self.builder.images_to_update() == {c, d}
        self.builder.glob = "*a:*"
        assert self.builder.images_to_update() == {a, b, c, d}

    @patch("docker_pkg.drivers.DockerDriver.exists")
    @patch("docker_pkg.image.DockerImage.build")
    @patch("docker_pkg.image.DockerImage.verify")
    def test_build(self, verify, build, exists):
        # Simple build
        exists.return_value = False
        # The first image builds correctly, the second one doesn't
        build.side_effect = [True, False]
        # Assume verification is successful
        verify.return_value = True
        img0 = ImageFSM(
            os.path.join(fixtures_dir, "foo-bar"), self.builder.client, self.builder.config
        )
        img1 = ImageFSM(
            os.path.join(fixtures_dir, "foobar-server"), self.builder.client, self.builder.config
        )
        self.builder.all_images = set([img0, img1])
        result = [r for r in self.builder.build()]
        # Check we did not pull the base image.
        self.builder.client.images.pull.assert_not_called()
        # Check we called verify only once, for the image correctly built
        self.assertEqual(verify.call_count, 1)
        # Check the results.
        self.assertEqual("foo-bar:0.0.1", result[0].label)
        self.assertEqual("verified", result[0].state)
        self.assertEqual("foobar-server:0.0.1~alpha1", result[1].label)
        self.assertEqual("error", result[1].state)

    @patch("docker_pkg.drivers.DockerDriver.exists")
    @patch("docker_pkg.image.DockerImage.build")
    @patch("docker_pkg.image.DockerImage.verify")
    def test_build_upstream_version(self, verify, build, exists):
        with patch("docker_pkg.drivers.DockerDriver.exists") as mocker:
            mocker.return_value = False
            self.builder.scan()

        exists.return_value = False
        build.return_value = True
        verify.return_value = True
        result = [r for r in self.builder.build()]
        dockerfile.TemplateEngine.setup({}, self.builder.known_images)

        for name, version in [
                ("upstream-version", "1.63.0-1"),
                ("upstream-version-extended", "1.63.0-1-20241211")]:
            upstream_version = [
                r for r in result if r.label == f"{name}:{version}"][0]
            self.assertEqual(f"{name}:{version}", upstream_version.label)
            self.assertEqual("verified", upstream_version.state)
            self.assertIn("ARG UPSTREAM_VERSION=1.63.0", upstream_version.image.render_dockerfile())

    @patch("docker_pkg.image.DockerImage.build")
    @patch("docker_pkg.image.DockerImage.verify")
    @patch("docker_pkg.builder.DockerBuilder.pull_dependencies")
    def test_build_pull(self, pull, verify, build):
        self.builder.pull = True
        verify.return_value = True

        def pull_result(img):
            if img.label == "foobar-server:0.0.1~alpha1":
                img.state = ImageFSM.STATE_ERROR

        pull.side_effect = pull_result
        img0 = ImageFSM(
            os.path.join(fixtures_dir, "foo-bar"), self.builder.client, self.builder.config
        )
        img1 = ImageFSM(
            os.path.join(fixtures_dir, "foobar-server"), self.builder.client, self.builder.config
        )
        img0.state = ImageFSM.STATE_TO_BUILD
        img1.state = ImageFSM.STATE_TO_BUILD
        build.return_value = True
        self.builder.all_images = set([img0, img1])
        result = [r for r in self.builder.build()]
        # Check we also pulled the base image
        self.builder.client.images.pull.assert_called_with("test")
        pull.assert_has_calls([call(img0), call(img1)])
        assert build.call_count == 1

    def test_pull_images(self):
        img0 = ImageFSM(
            os.path.join(fixtures_dir, "foo-bar"), self.builder.client, self.builder.config
        )
        img1 = ImageFSM(
            os.path.join(fixtures_dir, "foobar-server"), self.builder.client, self.builder.config
        )
        img0.state = ImageFSM.STATE_BUILT
        img1.state = ImageFSM.STATE_TO_BUILD
        self.builder.all_images = set([img0, img1])
        # img1 is locally built, but not published. No image should be pulled.
        self.builder.pull_dependencies(img1)
        self.builder.client.images.assert_not_called()
        # now if it's published, we should pull it instead
        img0.state = ImageFSM.STATE_PUBLISHED
        self.builder.pull_dependencies(img1)
        self.builder.client.images.pull.assert_called_with(img0.image.image)

    def test_images_in_state(self):
        img0 = ImageFSM(
            os.path.join(fixtures_dir, "foo-bar"), self.builder.client, self.builder.config
        )
        img1 = ImageFSM(
            os.path.join(fixtures_dir, "foobar-server"), self.builder.client, self.builder.config
        )
        img0.state = ImageFSM.STATE_BUILT
        img1.state = ImageFSM.STATE_ERROR
        self.builder.all_images = set([img0, img1])
        self.assertEqual([img0], self.builder.images_in_state(ImageFSM.STATE_BUILT))

    @patch("docker_pkg.image.DockerImage.verify")
    def test_publish(self, verify):
        self.builder.client.api = MagicMock()
        self.builder.config["username"] = None
        self.builder.config["password"] = None
        self.builder.config["registry"] = "example.org"
        with patch("docker_pkg.builder.ImageFSM._is_published") as mp:
            mp.return_value = False
            img0 = ImageFSM(
                os.path.join(fixtures_dir, "foo-bar"), self.builder.client, self.builder.config
            )
            img1 = ImageFSM(
                os.path.join(fixtures_dir, "foobar-server"),
                self.builder.client,
                self.builder.config,
            )
        # One image was already built, the other was verified.
        img0.state = "built"
        img1.state = "verified"
        self.builder.all_images = set([img0, img1])
        # No image gets published if no credentials are set.
        self.assertEqual([], [r for r in self.builder.publish()])
        self.assertEqual(self.builder.client.api.tag.call_count, 0)
        self.assertEqual(verify.call_count, 0)

        # Now with credentials set
        self.builder.config["username"] = "foo"
        self.builder.config["password"] = "bar"
        result = [r for r in self.builder.publish()]
        self.assertEqual(ImageFSM.STATE_PUBLISHED, result[1].state)
        self.builder.client.api.push.assert_any_call(
            "example.org/foobar-server",
            "0.0.1~alpha1",
            auth_config={"username": "foo", "password": "bar"},
        )
        # Only one image needed to be verified before publishing.
        self.assertEqual(verify.call_count, 1)
