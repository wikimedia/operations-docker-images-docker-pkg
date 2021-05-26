import os
from contextlib import contextmanager
from typing import Any, Dict, List

import attr
import docker.errors

from docker_pkg import ImageLabel, log


@contextmanager
def pushd(dirname: str):
    """
    Changes the current directory of execution.
    """
    cur_dir = os.getcwd()
    os.chdir(dirname)
    try:
        yield
    finally:
        os.chdir(cur_dir)


@attr.s
class DriverInterface:
    config: Dict[str, Any] = attr.ib()
    label: ImageLabel = attr.ib()

    def __str__(self) -> str:
        """String representation is <image_name>:<tag>"""
        return self.label.label("full")

    @property
    def buildargs(self) -> Dict[str, str]:
        proxy = self.config.get("http_proxy", None)
        if proxy is None:
            return {}
        return {
            "http_proxy": proxy,
            "https_proxy": proxy,
            "HTTP_PROXY": proxy,
            "HTTPS_PROXY": proxy,
        }

    # Methods that will need to be overridden in the actual drivers.

    def do_build(self, build_path: str, filename: str = "Dockerfile") -> str:
        """Given a build context and a dockerfile, build an image and return its label"""
        raise NotImplementedError("This method needs to be implemented by the driver.")

    def exists(self) -> bool:
        """Check if a container image exists locally."""
        return False

    def publish(self, tags: List[str]) -> bool:
        return True

    def clean(self):
        """Remove the current image"""
        raise NotImplementedError("This method needs to be implemented by the driver.")

    def prune(self):
        """Remove all old version of the selected image stored locally"""
        raise NotImplementedError("This method needs to be implemented by the driver.")

    def add_tag(self, lbl: ImageLabel, tag: str):
        """Add a new tag to a docker image"""
        raise NotImplementedError("This method needs to be implemented by the driver.")


@attr.s
class DockerDriver(DriverInterface):
    """Lower-level management of docker images"""

    config: Dict[str, Any] = attr.ib()
    label: ImageLabel = attr.ib()
    client: docker.client.DockerClient = attr.ib()
    nocache: bool = attr.ib(default=True)

    def do_build(self, build_path: str, filename: str = "Dockerfile") -> str:
        """
        Builds the image

        Parameters:
        build_path - context where the build must be performed
        filename - the file to output the generated dockerfile to

        Returns the image label
        Raises an error if the build fails
        """

        def stream_to_log(logger, chunk: Dict):
            if "error" in chunk:
                error_msg = chunk["errorDetail"]["message"].rstrip()
                error_code = chunk["errorDetail"].get("code", 0)
                if error_code != 0:

                    logger.error(
                        "Build command failed with exit code %s: %s", error_code, error_msg
                    )
                else:
                    logger.error("Build failed: %s", error_msg)
                raise docker.errors.BuildError(
                    "Building image {} failed".format(self.label.image()), logger
                )
            elif "stream" in chunk:
                logger.info(chunk["stream"].rstrip())
            elif "status" in chunk:
                if "progress" in chunk:
                    logger.debug("%s\t%s: %s ", chunk["status"], chunk["id"], chunk["progress"])
                else:
                    logger.info(chunk["status"])
            elif "aux" in chunk:
                # Extra information not presented to the user such as image
                # digests or image id after building.
                return
            else:
                logger.warning("Unhandled stream chunk: %s" % chunk)

        image_logger = log.getChild(self.label.image())
        with pushd(build_path):
            for line in self.client.api.build(
                path=build_path,
                dockerfile=filename,
                tag=self.label.image(),
                nocache=self.nocache,
                rm=True,
                pull=False,  # We manage pulling ourselves
                buildargs=self.buildargs,
                decode=True,
            ):
                stream_to_log(image_logger, line)
        return self.label.image()

    def clean(self):
        """Remove the image if needed"""
        try:
            self.client.images.remove(self.label.image())
        except docker.errors.ImageNotFound:
            pass

    def publish(self, tags) -> bool:
        """Publish a list of tags using docker push"""
        if not all(k in self.config for k in ["username", "password"]):
            raise ValueError("Cannot publish without credentials.")
        auth = {"username": self.config["username"], "password": self.config["password"]}
        for tag in tags:
            try:
                self.client.api.push(self.label.name(), tag, auth_config=auth)
            except docker.errors.APIError as e:
                log.error("Failed to publish image %s:%s: %s", self.label.label("full"), tag, e)
                return False
        return True

    def prune(self) -> bool:
        """
        Removes all old versions of the image from the local docker daemon.

        returns True if successful, False otherwise
        """
        success = True
        for image in self.client.images.list(self.label.name()):
            # If any of the labels correspond to what declared in the
            # changelog, keep it
            image_aliases = image.attrs["RepoTags"]
            if not any([(alias == self.label.image()) for alias in image_aliases]):
                try:
                    img_id = image.attrs["Id"]
                    log.info('Removing image "%s" (Id: %s)', image_aliases[0], img_id)
                    self.client.images.remove(img_id)
                except Exception as e:
                    log.error("Error removing image %s: %s", img_id, str(e))
                    success = False
        return success

    def exists(self) -> bool:
        """True if the image is present locally, false otherwise"""
        try:
            self.client.images.get(self.label.image())
            return True
        except docker.errors.ImageNotFound:
            return False

    def add_tag(self, label: ImageLabel, tag: str):
        self.client.api.tag(label.image(), label.name(), tag)


def get(config: Dict[str, Any], **kwargs) -> DriverInterface:
    """Factory method to get the driver."""
    driver_name = config.get("driver", "docker")
    empty_label = ImageLabel(config, "", "")
    if driver_name == "docker":
        if "client" not in kwargs:
            raise ValueError("You need to provide a docker client to the docker driver.")
        return DockerDriver(
            config, empty_label, kwargs["client"], nocache=kwargs.get("nocache", True)
        )
    else:
        raise ValueError("Driver {} not supported".format(driver_name))
