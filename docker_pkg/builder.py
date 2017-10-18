import os

import docker
import requests

from docker_pkg import image, log


class ImageFSM(object):

    STATES = ['built', 'to_build', 'published', 'error']

    def __init__(self, root, client, config):
        self.config = config
        self.image = image.DockerImage(root, client, self.config)
        if not self.image.exists():
            self.state = 'to_build'
        elif self._is_published():
            self.state = 'published'
        else:
            self.state = 'built'

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
        if self.config.get('registry', False):
            manifest_url = 'https://{registry}/{image}/manifests/{tag}'.format(
                registry=self.config['registry'],
                image=self.name,
                tag=self.image.tag
            )
            resp = requests.get(manifest_url)
            return (resp.status_code == requests.codes.ok)
        else:
            return False

    def build(self):
        """Build the image"""
        if self.state == 'built':
            return
        if self.state != 'to_build':
            raise ValueError(
                'Image {image} is already built or failed to build'.format(image=self.image))
        if self.image.build():
            self.state = 'built'
        else:
            self.state = 'error'

    def add_tag(self, tag):
        print('adding_tag %s' % tag)
        print("Call: %s %s %s" % (self.image.image, self.image.name, tag))
        self.image.docker.api.tag(self.image.image, self.image.name, tag)

    def publish(self):
        """Publish the image"""
        if self.state != 'built':
            raise ValueError(
                'Image {image} is not built, cannot publish it!'.format(image=self.image))
        # Checking the config has these keys should be done before trying to publish
        auth = {
            'username': self.config['username'],
            'password': self.config['password']
        }
        self.add_tag('latest')
        for tag in [self.image.tag, 'latest']:
            try:
                self.image.docker.api.push(self.image.name, tag, auth_config=auth)
                self.state = 'published'
            except docker.errors.APIError as e:
                log.error('Failed to publish image %s:%s: %s', self.image, tag, e)
                self.state = 'error'
                break


class DockerBuilder(object):
    """Scans the filesystem for image declarations, and build them"""

    def __init__(self, directory, config):
        if os.path.isabs(directory):
            self.root = directory
        else:
            self.root = os.path.join(os.getcwd(), directory)

        self.config = config
        self.client = docker.from_env(version='auto', timeout=600)
        # The build chain is the list of images we need to build,
        # while the other list is just a list of images we have a reference to
        #
        # TODO: fetch the available images on our default registry too?
        self.known_images = set(config.get('base_images', []))
        self.all_images = set()
        self._build_chain = []

    def scan(self):
        """
        Scan the desired directory for dockerfiles, add them all to a build chain
        """
        for root, dirs, files in os.walk(self.root):
            if 'Dockerfile.template' in files and 'changelog' in files:
                log.info('Processing the dockerfile template in %s', root)
                try:
                    img = ImageFSM(root, self.client, self.config)
                    self.known_images.add(img.label)
                    self.all_images.add(img)
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
        for img in self.images_in_state('to_build'):
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
            if dep_img is None or dep_img.state != 'to_build':
                continue
            # TODO: manage the case where the image state is 'error'
            # Add recursively any dependency of the image
            self._add_deps(dep_img)
        # If at this point the image is in the build chain, one of its
        # dependencies required it. This means we have a circular dependency.
        if image in self._build_chain:
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
        if not all([self.config['username'], self.config['password']]):
            log.warning('Cannot publish images if both username and password are not set')
            return
        for img in self.images_in_state('built'):
            img.publish()
            yield img
