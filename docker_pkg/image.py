"""
Manipulation of Docker images
"""

import datetime
import glob
import os
import random
import re
import shutil
import string
import subprocess
import tarfile
import tempfile
import time

from contextlib import contextmanager
from io import BytesIO

import docker.errors
from debian.changelog import Changelog
from debian.deb822 import Packages

from docker_pkg import dockerfile, log, image_fullname


@contextmanager
def pushd(dirname):
    """
    Changes the current directory of execution.
    """
    cur_dir = os.getcwd()
    os.chdir(dirname)
    try:
        yield
    finally:
        os.chdir(cur_dir)


class DockerImageBase(object):
    """Lower-level management of docker images"""
    def __init__(
        self, name, tag, client, config, directory, tpl, build_path,
            nocache=True
    ):
        self.config = config
        self.docker = client
        self.short_name = name
        self.tag = tag
        self.path = directory
        self.dockerfile_tpl = tpl
        self.build_path = build_path
        self.nocache = nocache

    @property
    def name(self):
        """Canonical Image name including registry and namespace"""
        return image_fullname(self.short_name, self.config)

    @property
    def safe_name(self):
        """A filesystem-friendly identified"""
        return self.short_name.replace('/', '-')

    def __str__(self):
        """String representation is <image_name>:<tag>"""
        return self.image

    @property
    def image(self):
        """Image label <name:tag>"""
        return "{name}:{tag}".format(name=self.name, tag=self.tag)

    def prune(self):
        """
        Removes all old versions of the image from the local docker daemon.

        returns True if successful, False otherwise
        """
        success = True
        for image in self.docker.images.list(self.name):
            # If any of the labels correspond to what declared in the
            # changelog, keep it
            image_aliases = image.attrs['RepoTags']
            if not any([(alias == self.image) for alias in image_aliases]):
                try:
                    img_id = image.attrs['Id']
                    log.info('Removing image "%s" (Id: %s)', image_aliases[0], img_id)
                    self.docker.images.remove(img_id)
                except Exception as e:
                    log.error('Error removing image %s: %s', img_id, str(e))
                    success = False
        return success

    def exists(self):
        """True if the image is present locally, false otherwise"""
        try:
            self.docker.images.get(self.image)
            return True
        except docker.errors.ImageNotFound:
            return False

    def extract(self, src, dst):
        """Extract a path from an image to the filesystem"""
        container_name = '{name}-ephemeral-{rand}'.format(
            name=self.short_name,
            rand="".join(random.choice(string.ascii_letters) for x in range(5))
        )
        success = False
        try:
            container = self.docker.containers.create(
                self.image,
                name=container_name,
                network_disabled=False  # see https://github.com/docker/docker-py/issues/1195
            )
            archive = container.get_archive(src)
            # Force the data to be read, so the docker connection will be freed
            data = archive[0].data
        except docker.errors.ImageNotFound:
            log.error("%s - image not found, cannot extract its contents", self.image)
        except Exception as e:
            log.error("%s - generic error during the extraction: %s", self.image, e)
        else:
            tar = tarfile.open(mode="r", fileobj=BytesIO(data))
            tar.extractall(path=dst)
            success = True
        finally:
            self.remove_container(container_name)
            if not success:
                raise RuntimeError('Building artifacts failed')

    def remove_container(self, name):
        """Removes the named container if it exists."""
        try:
            container = self.docker.containers.get(name)
        except docker.errors.NotFound:
            return False
        container.remove()
        return True

    @property
    def buildargs(self):
        proxy = self.config.get('http_proxy', None)
        if proxy is None:
            return {}
        return {'http_proxy': proxy, 'https_proxy': proxy,
                'HTTP_PROXY': proxy, 'HTTPS_PROXY': proxy}

    def build(self, build_path, filename='Dockerfile'):
        """
        Builds the image

        Parameters:
        build_path - context where the build must be performed
        filename - the file to output the generated dockerfile to

        Returns the image label
        Raises an error if the build fails
        """
        dockerfile = self.dockerfile_tpl.render(**self.config)
        log.info("Generated dockerfile for %s:\n%s", self.image, dockerfile)
        if dockerfile is None:
            raise RuntimeError('The generated dockerfile is empty')
        with open(os.path.join(build_path, filename), 'w') as fh:
            fh.write(dockerfile)

        def stream_to_log(logger, chunk):
            if 'error' in chunk:
                error_msg = chunk['errorDetail']['message'].rstrip()
                error_code = chunk['errorDetail'].get('code', 0)
                if error_code != 0:

                    logger.error('Build command failed with exit code %s: %s',
                                 error_code, error_msg)
                else:
                    logger.error('Build failed: %s', error_msg)
                raise docker.errors.BuildError('Building image {} failed'.format(self.image))
            elif 'stream' in chunk:
                logger.info(chunk['stream'].rstrip())
            elif 'status' in chunk:
                if 'progress' in chunk:
                    logger.debug("%s\t%s: %s ", chunk['status'], chunk['id'], chunk['progress'])
                else:
                    logger.info(chunk['status'])
            elif 'aux' in chunk:
                # Extra information not presented to the user such as image
                # digests or image id after building.
                return
            else:
                logger.warning('Unhandled stream chunk: %s' % chunk)

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
                    decode=True):
                stream_to_log(image_logger, line)
        return self.image

    def clean(self):
        """Remove the image if needed"""
        try:
            self.docker.images.remove(self.image)
        except docker.errors.ImageNotFound:
            pass


class DockerImage(DockerImageBase):
    """
    High-level management of docker images.

    If a Dockerfile.build.template is present, the build image will
    be built first, and artifacts will be extracted from it
    """

    TEMPLATE = 'Dockerfile.template'
    BUILD_TEMPLATE = 'Dockerfile.build.template'
    NIGHTLY_BUILD_FORMAT = '%Y%m%d-%H%M%S'
    is_nightly = False

    def __init__(
        self, directory, client, config,
        nocache=True
    ):
        self.metadata = {}
        self.read_metadata(directory)
        tpl = dockerfile.from_template(directory, self.TEMPLATE)
        # The build path will be set later
        super().__init__(
            self.metadata['name'], self.metadata['tag'],
            client, config, directory, tpl, None, nocache
        )
        # Now instantiate the build image, if needed
        if os.path.isfile(os.path.join(directory, self.BUILD_TEMPLATE)):
            build_tpl = dockerfile.from_template(directory, self.BUILD_TEMPLATE)
            self.build_image = DockerImageBase(
                '{name}-build'.format(name=self.short_name), self.tag,
                self.docker, config, self.path, build_tpl, None)
        else:
            self.build_image = None

    def read_metadata(self, path):
        metadata = {}
        with open(os.path.join(path, 'changelog'), 'rb') as fh:
            changelog = Changelog(fh)
        deps = []
        try:
            with open(os.path.join(path, 'control'), 'rb') as fh:
                # deb822 might uses python-apt however it is not available
                # on pypi. Thus skip using apt_pkg.
                for pkg in Packages.iter_paragraphs(fh, use_apt_pkg=False):
                    for k in ['Build-Depends', 'Depends']:
                        deps_str = pkg.get(k, '')
                        if deps_str:
                            # TODO: support versions? not sure it's needed
                            deps.extend(re.split(r'\s*,[\s\n]*', deps_str))
        except FileNotFoundError:
            # no control file. we can live with that for now.
            pass
        self.metadata['depends'] = deps
        self.metadata['tag'] = str(changelog.get_version())
        if self.is_nightly:
            self.metadata['tag'] += '-{date}'.format(
                date=datetime.datetime.now().strftime(self.NIGHTLY_BUILD_FORMAT))
        self.metadata['name'] = str(changelog.get_package())
        return metadata

    def new_tag(self, identifier='s'):
        """Create a new version tag from the currently read tag"""
        # Note: this only supports a subclass of all valid debian tags.
        if identifier is None:
            identifier = ''
        re_new_version = re.compile(
            r'^([\d\-\.]+?)(-{}(\d+))?$'.format(identifier)
        )
        previous_version = self.metadata['tag']
        m = re_new_version.match(previous_version)
        if not m:
            raise ValueError("Was not able to match version {}}".format(previous_version))
        base, _, seqnum = m.groups()
        if seqnum is None:
            # Native version - we can just attach a version number
            seqnum = 0
        seqnum = int(seqnum) + 1
        return '{base}-{sep}{num}'.format(base=base, sep=identifier, num=seqnum)

    def create_update(self, reason, version=None):
        if version is None:
            version = self.new_tag(identifier=self.config['update_id'])
        changelog_name = os.path.join(self.path, 'changelog')
        with open(changelog_name, 'rb') as fh:
            changelog = Changelog(fh)
        fn, email = self._get_author()

        changelog.new_block(
            package=self.short_name,
            version=version,
            distributions=self.config['distribution'],
            urgency='high',
            author="{} <{}>".format(fn, email),
            date=time.strftime("%a, %e %b %Y %H:%M:%S %z"),
        )
        changelog.add_change('')
        for line in reason.split("\n"):
            changelog.add_change('   {}'.format(line))
        changelog.add_change('')
        with open(changelog_name, 'w') as fh:
            changelog.write_to_open_file(fh)

    def _get_author(self):
        """
        Gets the author name and email for an update.
        It uses the DEBFULLNAME and DEBEMAIL env variables if available, else
        it reverts to using git configuration data. Finally, a fallback is used,
        provided by the docker-pkg configuration.
        """
        name = self.config['fallback_author']
        email = self.config['fallback_email']
        git_failed = False
        if 'DEBFULLNAME' in os.environ:
            name = os.environ['DEBFULLNAME']
        else:
            try:
                name = subprocess.check_output(
                    ['git', 'config', '--get', 'user.name']).rstrip().decode('utf-8')
            except Exception:
                git_failed = True

        if 'DEBEMAIL' in os.environ:
            email = os.environ['DEBEMAIL']
        elif not git_failed:
            email = subprocess.check_output(
                ['git', 'config', '--get', 'user.email']).rstrip().decode('utf-8')
        return (name, email)

    @property
    def depends(self):
        return self.metadata['depends']

    def build(self):
        """
        Build the image.

        returns True if successful, False otherwise
        """
        success = False
        self._create_build_environment()
        try:
            if self._build_artifacts():
                log.info('%s - buiding the image', self.image)
                super().build(self.build_path)
                success = True
        except (docker.errors.BuildError, docker.errors.APIError) as e:
            log.exception("Building image %s failed - check your Dockerfile: %s", self.image, e)
        except Exception as e:
            log.exception('Unexpected error building image %s: %s', self.image, e)
        finally:
            self._clean_build_environment()
        return success

    def _build_artifacts(self):
        """
        Build the artifacts from the build dockerfile.

        returns True if successful, False otherwise
        """
        if self.build_image is None:
            return True
        success = False
        try:
            log.info('%s - building artifacts', self.image)
            log.info('%s - creating the build image %s', self.image, self.build_image)
            self.build_image.build(self.build_path, filename='Dockerfile.build')
            log.info('%s - extracting artifacts from the build image')
            self.build_image.extract('/build', self.build_path)
            success = True
        except (docker.errors.BuildError, docker.errors.APIError) as e:
            log.exception("Building image %s failed - check your Dockerfile: %s",
                          self.build_image, e)
        except Exception as e:
            log.exception('Unexpected error buildining artifacts for image %s: %s', self.image, e)
        finally:
            self.build_image.clean()
        return success

    def _dockerignore(self):
        dockerignore = os.path.join(self.path, '.dockerignore')
        ignored = []
        if not os.path.isfile(dockerignore):
            return None
        with open(dockerignore, 'r') as fh:
            for line in fh.readlines():
                # WARNING: does NOT support inline comments
                if line.startswith('#'):
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
        base = tempfile.mkdtemp(prefix='docker-pkg-{name}'.format(name=self.safe_name))
        build_path = os.path.join(base, 'context')

        shutil.copytree(self.path, build_path, ignore=self._dockerignore())
        self.build_path = build_path

    def _clean_build_environment(self):
        if self.build_path is not None:
            base = os.path.dirname(self.build_path)
            if os.path.isdir(base):
                log.info('Removing build context %s', base)
                shutil.rmtree(base)
