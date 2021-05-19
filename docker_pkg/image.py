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

from docker_pkg import ImageLabel, dockerfile, drivers, log


class DockerImage:
    """
    High-level management of docker images.
    """

    NIGHTLY_BUILD_FORMAT = "%Y%m%d"
    is_nightly = False

    def __init__(
        self,
        directory: str,
        driver: drivers.DriverInterface,
        config: Dict[str, Any],
    ):
        self.metadata: Dict[str, Any] = {}
        self.read_metadata(directory)
        self.config = config
        self.label = ImageLabel(config, self.metadata["name"], self.metadata["tag"])
        self.path = directory
        # The build path will be set later
        self.build_path = None
        self.driver = driver
        self.driver.label = self.label

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

    def write_dockerfile(self, build_path: str) -> str:
        tpl = dockerfile.from_template(self.path, "Dockerfile.template")
        docker_file = tpl.render(**self.config)
        log.info("Generated dockerfile for %s:\n%s", self.label.image(), docker_file)
        if docker_file is None:
            raise RuntimeError("The generated dockerfile is empty")

        output_file = os.path.join(build_path, "Dockerfile")
        with open(output_file, "w") as fh:
            fh.write(docker_file)
        # Ensure the last USER instruction contains a numeric UID
        if self.config.get("force_numeric_user") and not dockerfile.has_numeric_user(docker_file):
            raise RuntimeError(
                'Last USER instruction with non-numeric user, see "force_numeric_user" config'
            )
        return output_file

    def build(self) -> bool:
        """
        Build the image.

        returns True if successful, False otherwise
        """
        success = False
        with self.build_environment() as build_path:
            try:
                filename = self.write_dockerfile(build_path)
                log.info("%s - buiding the image", self.image)
                self.driver.do_build(build_path, filename)
                success = True
            except (docker.errors.BuildError, docker.errors.APIError) as e:
                log.exception("Building image %s failed - check your Dockerfile: %s", self.image, e)
            except Exception as e:
                log.exception("Unexpected error building image %s: %s", self.image, e)
        return success

    def publish(self) -> bool:
        self._add_tag("latest")
        return self.driver.publish([self.label.version, "latest"])

    def _add_tag(self, tag: str):
        """Add a new tag to an image"""
        log.debug("Adding tag %s to image %s", tag, self.label.image())
        self.driver.add_tag(self.label, tag)

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
            run_env = {}
            # Inject the proxy env variables if we have an HTTP proxy defined.
            run_env.update(self.driver.buildargs)
            if os.environ.get("PATH", ""):
                run_env.update({"PATH": os.environ["PATH"]})
            subprocess.run(to_run, check=True, env=run_env)
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

    @contextmanager
    def build_environment(self):
        """Creates a temporary directory to use as the build enviornment"""
        bp = self._create_build_environment()
        try:
            yield bp
        finally:
            self._clean_build_environment(bp)

    def _create_build_environment(self):
        base = tempfile.mkdtemp(prefix="docker-pkg-{name}".format(name=self.safe_name))
        build_path = os.path.join(base, "context")

        shutil.copytree(self.path, build_path, ignore=self._dockerignore())
        return build_path

    def _clean_build_environment(self, build_path: str):
        base = os.path.dirname(build_path)
        if os.path.isdir(base):
            log.info("Removing build context %s", base)
            shutil.rmtree(base)
