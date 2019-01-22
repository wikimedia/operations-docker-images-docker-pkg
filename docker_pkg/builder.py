"""
Workflow to process, build and publish image definitions
"""
import fnmatch
import os

import docker
import requests

from concurrent.futures import ThreadPoolExecutor

from docker_pkg import image, log


class ImageFSM(object):
    """
    Finite state machine
    """

    STATE_PUBLISHED = 'published'
    'Image is in the target Docker registry'

    STATE_BUILT = 'built'
    'Image is in the local Docker daemon'

    STATE_TO_BUILD = 'to_build'
    'Image could not be found in the registry or the daemon'

    STATE_ERROR = 'error'
    ('Error state, for examples a failure to build locally or to publish it '
     'to the registry')

    STATES = [STATE_PUBLISHED, STATE_BUILT, STATE_TO_BUILD, STATE_ERROR]
    'List of possible states'

    def __init__(self, root, client, config, nocache=True, pull=True):
        self.config = config
        self.image = image.DockerImage(root, client, self.config, nocache=nocache, pull=pull)
        self.state = self.STATE_TO_BUILD
        if pull:
            # If we allow docker to pull images from the registry,
            # we want to know if an image is already available and
            # not rebuild it.
            if self._is_published():
                self.state = self.STATE_PUBLISHED
            elif self.image.exists():
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

    @property
    def label(self):
        return self.image.image

    @property
    def name(self):
        return self.image.name

    def __repr__(self):
        return 'ImageFSM({label}, {state})'.format(label=self.label, state=self.state)

    def _is_published(self):
        """Check the registry for the image"""
        proxies = {
            'https': self.config.get('http_proxy', None)
        }
        if self.config.get('registry', False):
            url = 'https://{registry}/v2'.format(registry=self.config['registry'])
        else:
            # TODO: support dockerhub somehow!
            # Probably will need a different strategy there.
            return False
        if self.config.get('namespace', False):
            url += '/{}'.format(self.config['namespace'])
        url += '/{}'.format(self.image.short_name)
        manifest_url = '{url}/manifests/{tag}'.format(
            url=url,
            tag=self.image.tag,
        )
        resp = requests.get(manifest_url, proxies=proxies)
        return (resp.status_code == requests.codes.ok)

    def build(self):
        """Build the image"""
        if self.state == self.STATE_BUILT:
            return
        if self.state != self.STATE_TO_BUILD:
            raise ValueError(
                'Image {image} is already built or failed to build'.format(image=self.image))
        if self.image.build():
            self.add_tag('latest')
            self.state = self.STATE_BUILT
        else:
            self.state = self.STATE_ERROR

    def add_tag(self, tag):
        log.debug('Adding tag %s to image %s', tag, self.image.image)
        self.image.docker.api.tag(self.image.image, self.image.name, tag)

    def publish(self):
        """Publish the image"""
        if self.state != self.STATE_BUILT:
            raise ValueError(
                'Image {image} is not built, cannot publish it!'.format(image=self.image))
        # Checking the config has these keys should be done before trying to publish
        auth = {
            'username': self.config['username'],
            'password': self.config['password']
        }
        for tag in [self.image.tag, 'latest']:
            try:
                self.image.docker.api.push(self.image.name, tag, auth_config=auth)
                self.state = self.STATE_PUBLISHED
            except docker.errors.APIError as e:
                log.error('Failed to publish image %s:%s: %s', self.image, tag, e)
                self.state = self.STATE_ERROR
                break


class DockerBuilder(object):
    """Scans the filesystem for image declarations, and build them"""

    def __init__(self, directory, config, selection=None, nocache=True, pull=True):
        if os.path.isabs(directory):
            self.root = directory
        else:
            self.root = os.path.join(os.getcwd(), directory)

        self.config = config
        self.glob = selection
        self.nocache = nocache
        # Protect against trying to pull your own images from dockerhub.
        if self.config.get('registry') is None:
            if pull:
                log.warning('Not pulling images remotely as no registry is defined.')
            self.pull = False
        else:
            self.pull = pull
        self.client = docker.from_env(version='auto', timeout=600)
        # The build chain is the list of images we need to build,
        # while the other list is just a list of images we have a reference to
        #
        # TODO: fetch the available images on our default registry too?
        self.known_images = set(config.get('base_images', []))
        self.all_images = set()
        self._build_chain = []

    def _matches_glob(self, img):
        """
        Check if the label of an image matches the glob pattern
        """
        return (self.glob is None or fnmatch.fnmatch(img.label, self.glob))

    def scan(self, max_workers=1):
        """
        Scan the desired directory for dockerfiles, add them all to a build chain

        max_workers: maximum number of threads to use when scanning local
        definition of images. For each image found, ``scan`` triggers queries
        to the local Docker daemon and the registry. Passed to
        concurrent.futures.ThreadPoolExecutor(). Default: 1.
        """

        roots = []
        for root, dirs, files in os.walk(self.root):
            hasTemplate = 'Dockerfile.template' in files
            hasChangelog = 'changelog' in files

            if not hasTemplate and not hasChangelog:
                continue
            elif not hasTemplate:
                log.warning('Ignoring %s since it lacks a Dockerfile.template', root)
                continue
            elif not hasChangelog:
                log.warning('Ignoring %s since it lacks a changelog', root)
                continue
            # We have both files and can proceed this directory
            roots.append(root)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            imgs = executor.map(self._process_dockerfile_template, roots)

        for img in imgs:
            self.known_images.add(img.label)
            self.all_images.add(img)

    def _process_dockerfile_template(self, root):
        log.info('Processing the dockerfile template in %s', root)
        try:
            return ImageFSM(root, self.client, self.config, self.nocache, self.pull)
        except Exception as e:
            log.error('Could not load image in %s: %s', root, e, exc_info=True)
            raise RuntimeError(
                'The image in {d} could not be loaded, '
                'check the logs for details'.format(d=root))

    def images_in_state(self, state):
        """Find all images in a specific state"""
        if state not in ImageFSM.STATES:
            raise ValueError("Invalid state {s}".format(s=state))
        return [img for img in self.all_images if img.state == state]

    @property
    def build_chain(self):
        # reset the build chain
        self._build_chain = []
        for img in self.images_in_state(ImageFSM.STATE_TO_BUILD):
            if self._matches_glob(img):
                self._add_deps(img)
        return self._build_chain

    def _add_deps(self, img):
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
            raise RuntimeError('Dependency loop detected for image {image}'.format(image=img.image))
        self._build_chain.append(img)

    def _img_from_name(self, name):
        """Retrieve an image given a name"""
        for img in self.all_images:
            if img.image.short_name == name:
                return img
        return None

    def build(self):
        """Build the images in the build chain"""
        for img in self.build_chain:
            img.build()
            yield img

    def publish(self):
        """Publish all images to the configured registry"""
        if self.config.get('registry') is None:
            log.warning('Cannot publish if no registry is defined')
            return
        if not all([self.config['username'], self.config['password']]):
            log.warning('Cannot publish images if both username and password are not set')
            return
        for img in self.images_in_state(ImageFSM.STATE_BUILT):
            img.publish()
            yield img
