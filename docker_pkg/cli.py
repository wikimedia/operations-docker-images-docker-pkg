import argparse
import logging

import yaml

from docker_pkg import builder, dockerfile, image

defaults = {
    'registry': None,
    'username': None, 'password': None,
    'seed_image': 'wikimedia-stretch:latest',
    'apt_options': '',
    'http_proxy': None,
    'base_images': [],
    'namespace': None,
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--configfile', default="config.yaml")
    parser.add_argument('directory', help='The directory to scan for images')
    parser.add_argument('--debug', action='store_true', help='Activate debug logging')
    parser.add_argument('--nightly', action='store_true',
                        help='Prepare a nightly build')
    parser.add_argument('--select', metavar='GLOB', help='A glob pattern for the images to build',
                        default=None)
    return parser.parse_args()


def read_config(configfile):
    with open(configfile, 'rb') as fh:
        raw_config = yaml.safe_load(fh)
    if raw_config:
        defaults.update(raw_config)
    return defaults


def main(args=None):
    if args is None:
        args = parse_args()
    logfmt = "%(asctime)s [docker-pkg-build] %(levelname)s - %(message)s (%(filename)s:%(lineno)s)"  # noqa: E501
    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format=logfmt)
    else:
        logging.basicConfig(
            level=logging.INFO,
            filename='./docker-pkg-build.log',
            format=logfmt
        )
    # Nightly image building support
    image.DockerImage.is_nightly = args.nightly
    config = read_config(args.configfile)
    build = builder.DockerBuilder(args.directory, config, args.select)
    dockerfile.TemplateEngine.setup(config, build.known_images)
    print("== Step 0: scanning {d} ==".format(d=args.directory))
    build.scan()
    print("Will build the following images:")
    for img in build.build_chain:
        print("* {image}".format(image=img.label))

    print("== Step 1: building images ==")
    for img in build.build_chain:
        print("=> Building image {image}".format(image=img.label))
        img.build()
        if img.state != 'built':
            print(" ERROR: image {image} failed to build, see logs for details".format(
                image=img.name))
    # Publishing
    print("== Step 2: publishing ==")
    if not all([config['username'], config['password']]):
        print("NOT publishing images as we have no auth setup")
    else:
        for img in build.images_in_state('built'):
            img.publish()
            if img.state == 'published':
                print("Successfully published image {image}".format(image=img.name))

    print('== Build done! ==')
    if not args.debug:
        print("You can see the logs at ./docker-pkg-build.log")
