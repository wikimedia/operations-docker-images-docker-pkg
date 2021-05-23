"""
Manipulation of Docker images
"""

import datetime
import glob
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple

import docker.errors
from debian.changelog import Changelog
from debian.deb822 import Packages

from docker_pkg import dockerfile, log, ImageLabel


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


class DockerDriver:
    """Lower-level management of docker images"""

    def __init__(
        self,
        name: str,
        tag: str,
        client: docker.client.DockerClient,
        config: Dict[str, Any],
        directory: str,
        tpl: dockerfile.Template,
        build_path: Optional[str],
        nocache: bool = True,
    ):
        self.config = config
        self.docker = client
        self.short_name = name
        self.tag = tag
        self.label = ImageLabel(self.config, self.short_name, self.tag)
        # TODO: remove here.
        self.path = directory
        self.dockerfile_tpl = tpl
        self.build_path = build_path
        self.nocache = nocache

    @property
    def name(self) -> str:
        """Canonical Image name including registry and namespace"""
        return self.label.label()

    def __str__(self) -> str:
        """String representation is <image_name>:<tag>"""
        return self.label.label("full")

    @property
    def image(self) -> str:
        """Image label <name:tag>"""
        return self.label.label("full")

    def prune(self) -> bool:
        """
        Removes all old versions of the image from the local docker daemon.

        returns True if successful, False otherwise
        """
        success = True
        for image in self.docker.images.list(self.name):
            # If any of the labels correspond to what declared in the
            # changelog, keep it
            image_aliases = image.attrs["RepoTags"]
            if not any([(alias == self.image) for alias in image_aliases]):
                try:
                    img_id = image.attrs["Id"]
                    log.info('Removing image "%s" (Id: %s)', image_aliases[0], img_id)
                    self.docker.images.remove(img_id)
                except Exception as e:
                    log.error("Error removing image %s: %s", img_id, str(e))
                    success = False
        return success

    def exists(self) -> bool:
        """True if the image is present locally, false otherwise"""
        try:
            self.docker.images.get(self.image)
            return True
        except docker.errors.ImageNotFound:
            return False

    def remove_container(self, name: str) -> bool:
        """Removes the named container if it exists."""
        try:
            container = self.docker.containers.get(name)
        except docker.errors.NotFound:
            return False
        container.remove()
        return True

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

    def do_build(self, build_path: Optional[str], filename: str = "Dockerfile") -> str:
        """
        Builds the image

        Parameters:
        build_path - context where the build must be performed
        filename - the file to output the generated dockerfile to

        Returns the image label
        Raises an error if the build fails
        """
        # Typically happens if do_build is called before the build environment has been created
        if build_path is None:
            raise RuntimeError("No build path was defined.")
        docker_file = self.dockerfile_tpl.render(**self.config)
        log.info("Generated dockerfile for %s:\n%s", self.image, docker_file)
        if docker_file is None:
            raise RuntimeError("The generated dockerfile is empty")

        # Ensure the last USER instruction contains a numeric UID
        if self.config.get("force_numeric_user") and not dockerfile.has_numeric_user(docker_file):
            raise RuntimeError(
                'Last USER instruction with non-numeric user, see "force_numeric_user" config'
            )

        with open(os.path.join(build_path, filename), "w") as fh:
            fh.write(docker_file)

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
                    "Building image {} failed".format(self.image), logger
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

        image_logger = log.getChild(self.image)
        with pushd(build_path):
            for line in self.docker.api.build(
                path=build_path,
                dockerfile=filename,
                tag=self.image,
                nocache=self.nocache,
                rm=True,
                pull=False,  # We manage pulling ourselves
                buildargs=self.buildargs,
                decode=True,
            ):
                stream_to_log(image_logger, line)
        return self.image

    def clean(self):
        """Remove the image if needed"""
        try:
            self.docker.images.remove(self.image)
        except docker.errors.ImageNotFound:
            pass


class DockerImage:
    """
    High-level management of docker images.
    """

    TEMPLATE = "Dockerfile.template"
    NIGHTLY_BUILD_FORMAT = "%Y%m%d"
    is_nightly = False

    def __init__(
        self,
        directory: str,
        client: docker.client.DockerClient,
        config: Dict[str, Any],
        nocache: bool = True,
    ):
        self.metadata: Dict[str, Any] = {}
        self.read_metadata(directory)
        self.config = config
        self.label = ImageLabel(config, self.metadata["name"], self.metadata["tag"])
        tpl = dockerfile.from_template(directory, self.TEMPLATE)
        self.path = directory
        # The build path will be set later
        self.build_path = None
        self.driver = DockerDriver(
            self.metadata["name"],
            self.metadata["tag"],
            client,
            config,
            directory,
            tpl,
            None,
            nocache,
        )

    @property
    def short_name(self) -> str:
        return self.label.short_name

    @property
    def image(self) -> str:
        return self.label.label("full")

    @property
    def name(self) -> str:
        """Canonical Image name including registry and namespace"""
        return self.label.label("name")

    @property
    def safe_name(self):
        """A filesystem-friendly identified"""
        return self.short_name.replace("/", "-")

    @property
    def tag(self) -> str:
        return self.label.version

    def exists(self) -> bool:
        return self.driver.exists()

    def read_metadata(self, path: str):
        with open(os.path.join(path, "changelog"), "rb") as fh:
            changelog = Changelog(fh)
        deps = []
        try:
            with open(os.path.join(path, "control"), "rb") as fh:
                # deb822 might uses python-apt however it is not available
                # on pypi. Thus skip using apt_pkg.
                for pkg in Packages.iter_paragraphs(fh, use_apt_pkg=False):
                    for k in ["Build-Depends", "Depends"]:
                        deps_str = pkg.get(k, "")
                        if deps_str:
                            # TODO: support versions? not sure it's needed
                            deps.extend(re.split(r"\s*,[\s\n]*", deps_str))
        except FileNotFoundError:
            # no control file. we can live with that for now.
            pass
        self.metadata["depends"] = deps
        self.metadata["tag"] = str(changelog.version)
        if self.is_nightly:
            self.metadata["tag"] += "-{date}".format(
                date=datetime.datetime.now().strftime(self.NIGHTLY_BUILD_FORMAT)
            )
        self.metadata["name"] = str(changelog.get_package())

    def new_tag(self, identifier: Optional[str] = "s") -> str:
        """Create a new version tag from the currently read tag"""
        # Note: this only supports a subclass of all valid debian tags.
        if identifier is None:
            identifier = ""
        re_new_version = re.compile(r"^([\d\-\.]+?)(-{}(\d+))?$".format(identifier))
        previous_version = self.metadata["tag"]
        m = re_new_version.match(previous_version)
        if not m:
            raise ValueError("Was not able to match version {}".format(previous_version))
        base, _, seqnum = m.groups()
        if seqnum is None:
            # Native version - we can just attach a version number
            patch_num = 1
        else:
            patch_num = int(seqnum) + 1
        return "{base}-{sep}{num}".format(base=base, sep=identifier, num=patch_num)

    def create_update(self, reason: str, version: Optional[str] = None):
        if version is None:
            version = self.new_tag(identifier=self.config["update_id"])
        changelog_name = os.path.join(self.path, "changelog")
        with open(changelog_name, "rb") as fh:
            changelog = Changelog(fh)
        fn, email = self._get_author()

        changelog.new_block(
            package=self.short_name,
            version=version,
            distributions=self.config["distribution"],
            urgency="high",
            author="{} <{}>".format(fn, email),
            date=time.strftime("%a, %e %b %Y %H:%M:%S %z"),
        )
        changelog.add_change("")
        for line in reason.split("\n"):
            changelog.add_change("   {}".format(line))
        changelog.add_change("")
        with open(changelog_name, "w") as tfh:
            changelog.write_to_open_file(tfh)

    def _get_author(self) -> Tuple[str, str]:
        """
        Gets the author name and email for an update.
        It uses the DEBFULLNAME and DEBEMAIL env variables if available, else
        it reverts to using git configuration data. Finally, a fallback is used,
        provided by the docker-pkg configuration.
        """
        name = self.config["fallback_author"]
        email = self.config["fallback_email"]
        git_failed = False
        if "DEBFULLNAME" in os.environ:
            name = os.environ["DEBFULLNAME"]
        else:
            try:
                name = (
                    subprocess.check_output(["git", "config", "--get", "user.name"])
                    .rstrip()
                    .decode("utf-8")
                )
            except Exception:
                git_failed = True

        if "DEBEMAIL" in os.environ:
            email = os.environ["DEBEMAIL"]
        elif not git_failed:
            email = (
                subprocess.check_output(["git", "config", "--get", "user.email"])
                .rstrip()
                .decode("utf-8")
            )
        return (name, email)

    @property
    def depends(self) -> List[str]:
        return self.metadata["depends"]

    def build(self) -> bool:
        """
        Build the image.

        returns True if successful, False otherwise
        """
        success = False
        self._create_build_environment()
        try:
            log.info("%s - buiding the image", self.image)
            self.driver.do_build(self.build_path)
            success = True
        except (docker.errors.BuildError, docker.errors.APIError) as e:
            log.exception("Building image %s failed - check your Dockerfile: %s", self.image, e)
        except Exception as e:
            log.exception("Unexpected error building image %s: %s", self.image, e)
        finally:
            self._clean_build_environment()
        return success

    def verify(self) -> bool:
        """
        Verify the image.

        returns True if successful, or not test available
        """
        # We take the value of verify_args, and interpolate the current path and
        # the image full name into it. We also check for the existence of all the arguments that
        # are a filesystem path.
        for arg in self.config["verify_args"]:
            for part in shlex.split(arg):
                # Check if arguments that are in the path are indeed present on the filesystem.
                # If not, assume tests are not implemented for this image, and skip it quickly.
                if part.find("{path}") < 0:
                    continue
                path = part.format(path=self.path)
                if not os.path.exists(path):
                    log.info("Could not find path %s, skipping verification of %s", arg, self.name)
                    return True

        args = [el.format(image=self.name, path=self.path) for el in self.config["verify_args"]]
        try:
            executable = self.config["verify_command"]
            subprocess.run("which {}".format(executable), shell=True, check=True)
        except subprocess.CalledProcessError:
            log.error("Could not verify image %s: %s not found", self.name, executable)
            # This means that if the base executable we need isn't available, we will refuse to
            # publish any image.
            return False

        try:
            to_run = [executable] + args
            # Inject the proxy env variables if we have an HTTP proxy defined.
            subprocess.run(to_run, check=True, env=self.driver.buildargs)
            return True
        except subprocess.CalledProcessError as e:
            log.error(
                "Verification of image %s failed with return code %d", self.name, e.returncode
            )
            log.error("-- output: %s", e.stdout)
            return False

    def _dockerignore(self):
        dockerignore = os.path.join(self.path, ".dockerignore")
        ignored = []
        if not os.path.isfile(dockerignore):
            return None
        with open(dockerignore, "r") as fh:
            for line in fh.readlines():
                # WARNING: does NOT support inline comments
                if line.startswith("#"):
                    continue
                clean_line = line.strip()
                if not clean_line:
                    continue
                for filepath in glob.glob(os.path.join(self.path, clean_line)):
                    ignored.append(filepath)

        if not ignored:
            return None

        def _filter(src, files):
            return [f for f in files if (os.path.join(src, f) in ignored)]

        return _filter

    def _create_build_environment(self):
        if self.build_path is not None:
            # Build path already created, assume it's all good
            return
        base = tempfile.mkdtemp(prefix="docker-pkg-{name}".format(name=self.safe_name))
        build_path = os.path.join(base, "context")

        shutil.copytree(self.path, build_path, ignore=self._dockerignore())
        self.build_path = build_path

    def _clean_build_environment(self):
        if self.build_path is not None:
            base = os.path.dirname(self.build_path)
            if os.path.isdir(base):
                log.info("Removing build context %s", base)
                shutil.rmtree(base)
