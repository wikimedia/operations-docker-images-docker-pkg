import argparse
import logging
import sys

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

ACTIONS = ['build', 'prune']


def parse_args(args):
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--configfile', default="config.yaml")
    loglevel = parser.add_argument_group(title='logging options').add_mutually_exclusive_group()
    loglevel.add_argument('--debug', action='store_true', help='Activate debug logging')
    loglevel.add_argument('--info', action='store_true', help='Activate info logging')

    parser.add_argument('--nightly', action='store_true',
                        help='Prepare a nightly build')
    parser.add_argument('--select', metavar='GLOB', help='A glob pattern for the images to build',
                        default=None)

    build_opts = parser.add_argument_group('options for docker build')
    build_opts.add_argument('--use-cache', dest='nocache', action='store_false',
                            help='Do use Docker cache when building the images')
    build_opts.add_argument('--no-pull', dest='pull', action='store_false',
                            help='Do not attempt to pull a newer version of the images')
    parser.add_argument('directory', help='The directory to scan for images')
    # TODO: build a subparser maybe? And move some of the above parameters
    # to the build subparser only
    build_opts.add_argument('action', default='build', choices=ACTIONS, nargs='?')

    return parser.parse_args(args)


def read_config(configfile):
    with open(configfile, 'rb') as fh:
        raw_config = yaml.safe_load(fh)
    if raw_config:
        defaults.update(raw_config)
    return defaults


def main(args=None):
    log_to_stdout = True
    if args is None:
        args = parse_args(sys.argv[1:])
    logfmt = "%(asctime)s [docker-pkg-build] %(levelname)s - %(message)s (%(filename)s:%(lineno)s)"  # noqa: E501
    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format=logfmt)
    elif args.info:
        logging.basicConfig(level=logging.INFO, format=logfmt)
    else:
        log_to_stdout = False
        logging.basicConfig(
            level=logging.INFO,
            filename='./docker-pkg-build.log',
            format=logfmt
        )
    # Nightly image building support
    image.DockerImage.is_nightly = args.nightly
    config = read_config(args.configfile)
    application = builder.DockerBuilder(
        args.directory, config, args.select, args.nocache, args.pull)
    if args.action == 'build':
        build(application, log_to_stdout)
    elif args.action == 'prune':
        prune(application)
    else:
        raise ValueError(args.action)


def build(application, log_to_stdout):
    dockerfile.TemplateEngine.setup(application.config, application.known_images)
    print("== Step 0: scanning {d} ==".format(d=application.root))
    application.scan()
    print("Will build the following images:")
    for img in application.build_chain:
        print("* {image}".format(image=img.label))

    print("== Step 1: building images ==")
    for img in application.build_chain:
        print("=> Building image {image}".format(image=img.label))
        img.build()
        if img.state != builder.ImageFSM.STATE_BUILT:
            print(" ERROR: image {image} failed to build, see logs for details".format(
                image=img.name))
    # Publishing
    print("== Step 2: publishing ==")
    if not all([application.config['username'], application.config['password']]):
        print("NOT publishing images as we have no auth setup")
    else:
        for img in application.images_in_state(builder.ImageFSM.STATE_BUILT):
            img.publish()
            if img.state == builder.ImageFSM.STATE_PUBLISHED:
                print("Successfully published image {image}".format(image=img.name))

    print('== Build done! ==')
    if not log_to_stdout:
        print("You can see the logs at ./docker-pkg-build.log")


def prune(application):
    print("== Step 0: scanning {d} ==".format(d=application.root))
    application.scan()
    print("Will prune old versions of the following images:")
    for img in application.all_images:
        print("* {image}".format(image=img.label))

    print("== Step 1: pruning images")
    for img in application.all_images:
        if not img.prune():
            print("* Errors pruning old images for {}".format(img.label))
