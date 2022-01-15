"""
Workflow to process, build and publish image definitions
"""
import fnmatch
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Any, Dict, Generator, List, Optional, Set

import docker
import requests

from docker_pkg import drivers, image, log

mutex = Lock()


class ImageFSM:
    """
    Finite state machine
    """

    STATE_PUBLISHED = "published"
    "Image is in the target Docker registry"

    STATE_VERIFIED = "verified"
    "Image has been verified."

    STATE_BUILT = "built"
    "Image is in the local Docker daemon"

    STATE_TO_BUILD = "to_build"
    "Image could not be found in the registry or the daemon"

    STATE_ERROR = "error"
    ("Error state, for examples a failure to build locally or to publish it " "to the registry")

    STATES = [STATE_PUBLISHED, STATE_BUILT, STATE_TO_BUILD, STATE_VERIFIED, STATE_ERROR]
    "List of possible states"

    _instances: List[str] = []

    def __init__(
        self,
        root: str,
        client: docker.client.DockerClient,
        config: Dict,
        nocache: bool = True,
        pull: bool = True,
    ):
        self.config = config
        # Create a generic driver to inject in the image.
        driver = drivers.get(config, client=client, nocache=nocache)
        self.image = image.DockerImage(root, driver, self.config)

        self.state = self.STATE_TO_BUILD
        self.children: Set["ImageFSM"] = set()
        if pull:
            # If we allow docker to pull images from the registry,
            # we want to know if an image is already available and
            # not rebuild it.
            if self._is_published():
                self.state = self.STATE_PUBLISHED
            elif self.image.exists():
                # We always want to verify the image before publishing!
                self.state = self.STATE_BUILT
            else:
                self.state = self.STATE_TO_BUILD
        else:
            # If we're not allowing docker to pull images from the registry,
            # we need to build any image that's not already present.
            if not self.image.exists():
                self.state = self.STATE_TO_BUILD
            elif self._is_published():
                self.state = self.STATE_PUBLISHED
            else:
                self.state = self.STATE_BUILT
        # Register this FSM in the list of instances.
        # Check that we're not initializing a second instance of the FSM for the same
        # docker image.
        # Please note the check happens here, after all non-blocking io is finished, so
        # the GIL will lock execution in a single thread for us. Still, for clarity to the
        # reader, and for future-proofing the code here, we explicitly add a mutex.
        mutex.acquire()
        if self.image.short_name in ImageFSM._instances:
            mutex.release()
            raise RuntimeError(
                "Trying to reinstantiate the FSM for image {}".format(self.image.short_name)
            )
        else:
            ImageFSM._instances.append(self.image.short_name)
            mutex.release()

    @property
    def label(self) -> str:
        """The full label of the image, $registry/$ns/$name:$tag"""
        return self.image.image

    @property
    def name(self) -> str:
        """The image full name, $registry/$ns/$name"""
        return self.image.name

    def __repr__(self) -> str:
        return "ImageFSM({label}, {state})".format(label=self.label, state=self.state)

    def _is_published(self) -> bool:
        """Check the registry for the image"""
        proxies = {"https": self.config.get("http_proxy", None)}
        if self.config.get("registry", False):
            url = "https://{registry}/v2".format(registry=self.config["registry"])
        else:
            # TODO: support dockerhub somehow!
            # Probably will need a different strategy there.
            return False
        if self.config.get("namespace", False):
            url += "/{}".format(self.config["namespace"])
        url += "/{}".format(self.image.short_name)
        manifest_url = "{url}/manifests/{tag}".format(
            url=url,
            tag=self.image.tag,
        )
        resp = requests.get(manifest_url, proxies=proxies)
        return resp.status_code == requests.codes.ok

    def build(self):
        """Build the image"""
        if self.state == self.STATE_BUILT:
            return
        if self.state != self.STATE_TO_BUILD:
            raise ValueError(
                "Image {image} is already built or failed to build".format(image=self.image)
            )
        if self.image.build():
            self.state = self.STATE_BUILT
        else:
            self.state = self.STATE_ERROR

    def verify(self):
        """Verify the image."""
        if self.state == self.STATE_VERIFIED:
            return
        if self.state != self.STATE_BUILT:
            raise ValueError(
                "Image {image} cannot be verified as it's not built, or already published.".format(
                    image=self.image
                )
            )
        if self.image.verify():
            self.state = self.STATE_VERIFIED
        else:
            self.state = self.STATE_ERROR

    def publish(self):
        """Publish the image"""
        if self.state != self.STATE_VERIFIED:
            raise ValueError(
                "Image {image} is not verified, cannot publish it!".format(image=self.image)
            )
        if self.image.publish():
            self.state = self.STATE_PUBLISHED
        else:
            self.state = self.STATE_ERROR

    def add_child(self, img: "ImageFSM"):
        """Declare another image as child of the current one"""
        self.children.add(img)

    def all_children(self) -> List["ImageFSM"]:
        """
        Report all images that are born of the current one.

        The result will include the current image, and all of their direct and
        indirect children. No logical loop protection is present here, nor the
        ordering is guaranteed in any ways.

        Returns: (list) A list of all images that include the current one.
        """
        children = {self}
        # This just needs to be a list of images without
        # duplicates.
        for child in self.children:
            children.update(child.all_children())

        return list(children)


class DockerBuilder:
    """Scans the filesystem for image declarations, and build them"""

    def __init__(
        self,
        directory: str,
        config: Dict[str, Any],
        selection: Optional[str] = None,
        nocache: bool = True,
        pull: bool = True,
    ):
        if os.path.isabs(directory):
            self.root = directory
        else:
            self.root = os.path.join(os.getcwd(), directory)

        self.config = config
        self.glob = selection
        self.nocache = nocache
        # Protect against trying to pull your own images from dockerhub.
        if self.config.get("registry") is None:
            if pull:
                log.warning("Not pulling images remotely as no registry is defined.")
            self.pull = False
        else:
            self.pull = pull

        # Base images we need to refresh before building, see T219398, as labels.
        self.base_images: List[str] = config.get("base_images", [])

        self.client = docker.from_env(version="auto", timeout=600)
        # Perform a login if the credentials are provided
        if all(
            [self.config.get("username"), self.config.get("password"), self.config.get("registry")]
        ):
            self.client.login(
                username=self.config["username"],
                password=self.config["password"],
                registry="https://{}".format(self.config["registry"]),
                reauth=True,
            )

        # We create three lists here:
        # all_images is a set of all the ImageFSMs generated for the images we find in our scan
        # known_images is a list of full image labels (so, fullname:tag) that is a sum of what we
        # find in our scan and images we add to the configuration as *already known* images.
        # _build_chain is a list of the ImageFSM objects we need to build, in the correct build
        # sequence.
        # The build chain is the list of images we need to build,
        # while the other list is just a list of images we have a reference to
        #
        # TODO: fetch the available images on our default registry too?
        self.known_images: Set[str] = set(self.base_images)
        self.all_images: Set[ImageFSM] = set()
        self._build_chain: List[ImageFSM] = []

    def _matches_glob(self, img: ImageFSM) -> bool:
        """
        Check if the label of an image matches the glob pattern
        """
        return self.glob is None or fnmatch.fnmatch(img.label, self.glob)

    def scan(self, max_workers: int = 1):
        """
        Scan the desired directory for dockerfiles, add them all to a build chain

        max_workers: maximum number of threads to use when scanning local
        definition of images. For each image found, ``scan`` triggers queries
        to the local Docker daemon and the registry. Passed to
        concurrent.futures.ThreadPoolExecutor(). Default: 1.
        """

        roots = []
        for root, _, files in os.walk(self.root):
            hasTemplate = "Dockerfile.template" in files
            hasChangelog = "changelog" in files

            if not hasTemplate and not hasChangelog:
                continue
            elif not hasTemplate:
                log.warning("Ignoring %s since it lacks a Dockerfile.template", root)
                continue
            elif not hasChangelog:
                log.warning("Ignoring %s since it lacks a changelog", root)
                continue
            # We have both files and can proceed this directory
            roots.append(root)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            imgs = executor.map(self._process_dockerfile_template, roots)

        for img in imgs:
            self.known_images.add(img.label)
            self.all_images.add(img)

    def _process_dockerfile_template(self, root: str) -> ImageFSM:
        log.info("Processing the dockerfile template in %s", root)
        try:
            return ImageFSM(root, self.client, self.config, self.nocache, self.pull)
        except Exception as e:
            log.error("Could not load image in %s: %s", root, e, exc_info=True)
            raise RuntimeError(
                "The image in {d} could not be loaded, " "check the logs for details".format(d=root)
            )

    def images_in_state(self, state: str) -> List[ImageFSM]:
        """Find all images in a specific state"""
        if state not in ImageFSM.STATES:
            raise ValueError("Invalid state {s}".format(s=state))
        return [img for img in self.all_images if img.state == state]

    @property
    def build_chain(self) -> List[ImageFSM]:
        # reset the build chain
        self._build_chain = []
        for img in self.images_in_state(ImageFSM.STATE_TO_BUILD):
            if self._matches_glob(img):
                self._add_deps(img)
        return self._build_chain

    def prune_chain(self) -> List[ImageFSM]:
        """Returns the images that need to be pruned, in the correct order."""
        # This is a hack. We're abusing the build chain concept.
        for fsm in self.all_images:
            if self._matches_glob(fsm):
                fsm.state = ImageFSM.STATE_TO_BUILD
            elif fsm.state == ImageFSM.STATE_TO_BUILD:
                fsm.state = ImageFSM.STATE_BUILT
        chain = self.build_chain
        chain.reverse()
        return chain

    def _add_deps(self, img: ImageFSM):
        if img in self._build_chain:
            # the image is already in the build chain, no reason to re-add it.
            # Also, stop going down this tree again
            return
        for dep in img.image.depends:
            dep_img = self._img_from_name(dep)
            # If the parent image doesn't exist or doesn't need to be built,
            # go on.
            # TODO: fail if dependency is not found?
            if dep_img is None or dep_img.state != ImageFSM.STATE_TO_BUILD:
                continue
            # TODO: manage the case where the image state is 'error'
            # Add recursively any dependency of the image
            self._add_deps(dep_img)
        # If at this point the image is in the build chain, one of its
        # dependencies required it. This means we have a circular dependency.
        if img in self._build_chain:
            raise RuntimeError("Dependency loop detected for image {image}".format(image=img.image))
        self._build_chain.append(img)

    def pull_dependencies(self, fsm: ImageFSM):
        """Pulls all dependencies from the docker registry, if they're present"""
        for name in fsm.image.depends:
            dep_img = self._img_from_name(name)
            # TODO: fail if dependency is not found?
            if dep_img is None or dep_img.state != ImageFSM.STATE_PUBLISHED:
                continue
            try:
                log.info("Pulling image %s, dependency of %s", dep_img.image.image, fsm.image.name)
                self.client.images.pull(dep_img.image.image)
            except docker.errors.APIError as e:
                log.exception("Failed to pull image %s: %s", dep_img.image.name, e)
                fsm.state = ImageFSM.STATE_ERROR

    def _build_dependencies(self):
        """Builds the dependency tree between the images."""
        for img in self.all_images:
            for dep in img.image.depends:
                dep_img = self._img_from_name(dep)
                if dep_img is None:
                    raise RuntimeError(
                        "Image {} (dependency of {}) not found".format(dep, img.label)
                    )
                dep_img.add_child(img)

    def images_to_update(self) -> Set[ImageFSM]:
        """Returns a list of images to update"""
        self._build_dependencies()
        images_to_update = set()
        for img in self.all_images:
            if self._matches_glob(img):
                for to_update in img.all_children():
                    images_to_update.add(to_update)
        return images_to_update

    def update_images(
        self, images: Set[ImageFSM], reason: str, baseimg: str, version: Optional[str] = None
    ):
        """Update the changelog for the images provided"""
        dep_reason = "Refresh for update in parent image {}:\n{}".format(baseimg, reason)
        for img in images:
            if img.image.short_name == baseimg:
                # For the base image, we use the version chosen
                # on the command line, if any.
                img.image.create_update(reason, version=version)
            else:
                # On the other images, we use a generated change reason instead
                # and we only increment the minor version automatically.
                img.image.create_update(dep_reason)

    def _img_from_name(self, name: str) -> Optional[ImageFSM]:
        """Retrieve an image given a name"""
        for img in self.all_images:
            if img.image.short_name == name:
                return img
        return None

    def build(self) -> Generator[ImageFSM, None, None]:
        """Build the images in the build chain"""
        # First refresh the base images, to avoid using stale copies of them.
        # See T219398
        if self.pull:
            for name in self.base_images:
                log.info("Refreshing %s", name)
                self.client.images.pull(name)
        for img in self.build_chain:
            # If pull is defined, call pull_dependencies()
            if self.pull:
                self.pull_dependencies(img)
            # If we are in a different state now, just return
            # the image
            if img.state == ImageFSM.STATE_TO_BUILD:
                img.build()
                # We verify each image that we build.
                # This ensures we run verification at build time even if we won't publish.
                if img.state == ImageFSM.STATE_BUILT:
                    img.verify()

            yield img

    def publish(self) -> Generator[ImageFSM, None, None]:
        """Publish all images to the configured registry"""
        if self.config.get("registry") is None:
            log.warning("Cannot publish if no registry is defined")
            return
        if not all([self.config["username"], self.config["password"]]):
            log.warning("Cannot publish images if both username and password are not set")
            return
        # We have two types of images we might publish:
        # - Images we just built (which will be in STATE_VERIFIED)
        # - Images previously built but not published (which will be in STATE_BUILT)
        # We first verify all images in STATE_BUILT, then publish the ones that were verified.
        for img in self.images_in_state((ImageFSM.STATE_BUILT)):
            img.verify()

        for img in self.images_in_state(ImageFSM.STATE_VERIFIED):
            img.publish()
            yield img
