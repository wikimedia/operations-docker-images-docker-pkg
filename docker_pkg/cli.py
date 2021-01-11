"""
Command line interface
"""

import argparse
import logging
import os
import sys
from typing import Any, Dict, List, Optional

import yaml

from docker_pkg import builder, dockerfile, image

defaults: Dict[str, Any] = {
    "registry": None,
    "username": None,
    "password": None,
    "seed_image": "wikimedia-stretch:latest",
    "apt_options": "",
    "http_proxy": None,
    "base_images": [],
    "namespace": None,
    "scan_workers": 8,
    "fallback_author": "Author",
    "fallback_email": "email@domain",
    "distribution": "wikimedia",
    "update_id": "s",
    "ca_bundle": None,
}

ACTIONS: List[str] = ["build", "prune", "update"]


def parse_args(args: List[str]):
    """Parse the command-line arguments."""
    parser = argparse.ArgumentParser()
    # Global options
    parser.add_argument("-c", "--configfile", default="config.yaml")
    loglevel = parser.add_argument_group(title="logging options").add_mutually_exclusive_group()
    loglevel.add_argument("--debug", action="store_true", help="Activate debug logging")
    loglevel.add_argument("--info", action="store_true", help="Activate info logging")

    actions = parser.add_subparsers(
        help="Action to perform: {}".format(",".join(ACTIONS)), dest="mode"
    )

    # Build selected images from a directory
    # Cli usage: docker-pkg -c test.yaml --info build --select "*python*" images_dir
    build = actions.add_parser("build", help="Build images (and publish them to the registry)")
    build_opts = build.add_argument_group("options for docker build")
    nightly = build_opts.add_argument_group(title="nightly").add_mutually_exclusive_group()
    nightly.add_argument("--nightly", action="store_true", help="Prepare a nightly build")
    nightly.add_argument("--snapshot", action="store_true", help="Create a snapshot build")
    build_opts.add_argument(
        "--use-cache",
        dest="nocache",
        action="store_false",
        help="Do use Docker cache when building the images",
    )
    build_opts.add_argument(
        "--no-pull",
        dest="pull",
        action="store_false",
        help="Do not attempt to pull a newer version of the images",
    )
    build_opts.add_argument(
        "--select",
        metavar="GLOB",
        help="A glob pattern for the images to build, must match name:tag",
        default=None,
    )
    # Prune the old versions of images from the local docker daemon.
    # Cli usage: docker-pkg prune --select "*nodejs*" --nightly images_dir
    prune = actions.add_parser("prune", help="Prune local outdated versions of images in DIRECTORY")
    prune.add_argument(
        "--select",
        metavar="GLOB",
        help="A glob pattern for the images to build, must match name:tag",
        default=None,
    )
    prune.add_argument(
        "--nightly",
        default=False,
        metavar="NIGHTLY_IDENTIFIER",
        help="Prune all but the nightly build indicated in the argument",
    )

    # Create an update for a specific image and all of their children.
    # Cli usage: docker-pkg update python3-dev --reason "Adding newer pip version" images_dir
    update = actions.add_parser("update", help="Helper for preparing an update of an image tree")
    update.add_argument("select", help="Names of the base image being updated", metavar="NAME")
    update.add_argument("--reason", help="Reason for the update.", default="Security update")
    update.add_argument(
        "--version", "-v", help="Specify a version for the image to upgrade", default=None
    )
    # The directory argument always goes last. We add it to every subparser to avoid a bad UX when
    # omitting it. See T253131
    for subp in [update, prune, build]:
        subp.add_argument("directory", metavar="DIRECTORY", help="The directory to scan for images")
    return parser.parse_args(args)


def read_config(configfile: str):
    config = defaults.copy()

    with open(configfile, "rb") as fh:
        raw_config = yaml.safe_load(fh)
    if raw_config:
        config.update(raw_config)
    return config


def main(args: Optional[argparse.Namespace] = None):
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
        logging.basicConfig(level=logging.INFO, filename="./docker-pkg-build.log", format=logfmt)

    config = read_config(args.configfile)
    # Force requests to use the configured ca bundle.
    if config["ca_bundle"] is not None:
        os.environ["REQUESTS_CA_BUNDLE"] = config["ca_bundle"]
    # Args mangling.

    select = args.select
    args_table = vars(args)
    nocache = args_table.get("nocache", True)
    # Prune and update don't need to pull!
    pull = args_table.get("pull", False)
    nightly_opt = args_table.get("nightly", False)
    is_snapshot = args_table.get("snapshot", False)
    if nightly_opt:
        image.DockerImage.is_nightly = True
    elif is_snapshot:
        # We're building a snapshot.
        image.DockerImage.is_nightly = True
        image.DockerImage.NIGHTLY_BUILD_FORMAT = "%Y%m%d-%H%M%S"
    if args.mode == "update":
        # For updates, we only allow literal names.
        select = "*{}:*".format(args.select)

    application = builder.DockerBuilder(args.directory, config, select, nocache, pull)
    dockerfile.TemplateEngine.setup(application.config, application.known_images)
    if args.mode == "build":

        build(application, log_to_stdout)
    elif args.mode == "prune":
        prune(application, nightly_opt)
    elif args.mode == "update":
        update(application, args.reason, args.select, args.version)
    else:
        raise ValueError(args.action)


def build(application: builder.DockerBuilder, log_to_stdout: bool):
    print("== Step 0: scanning {d} ==".format(d=application.root))
    application.scan(max_workers=application.config["scan_workers"])
    print("Will build the following images:")
    for img in application.build_chain:
        print("* {image}".format(image=img.label))

    print("== Step 1: building images ==")
    for img in application.build_chain:
        print("=> Building image {image}".format(image=img.label))
        img.build()
        if img.state != builder.ImageFSM.STATE_BUILT:
            print(
                " ERROR: image {image} failed to build, see logs for details".format(image=img.name)
            )
    # Publishing
    print("== Step 2: publishing ==")
    if not all([application.config["username"], application.config["password"]]):
        print("NOT publishing images as we have no auth setup")
    else:
        for img in application.images_in_state(builder.ImageFSM.STATE_BUILT):
            img.publish()
            if img.state == builder.ImageFSM.STATE_PUBLISHED:
                print("Successfully published image {image}".format(image=img.name))

    print("== Build done! ==")
    if not log_to_stdout:
        print("You can see the logs at ./docker-pkg-build.log")


def prune(application: builder.DockerBuilder, nightly: str):
    # cheat dockerimage into using a fixed format
    if nightly:
        image.DockerImage.NIGHTLY_BUILD_FORMAT = nightly
    print("== Step 0: scanning {d} ==".format(d=application.root))
    application.scan(max_workers=application.config["scan_workers"])
    # Let's peform a trick to be able to exploit the build_chain
    print("Will prune old versions of the following images:")
    pc = application.prune_chain()
    for fsm in pc:
        print("* {image}".format(image=fsm.label))

    print("== Step 1: pruning images")
    for fsm in pc:
        print("* Pruning old versions of {}".format(fsm.label))
        if not fsm.image.prune():
            print("* Errors pruning old images for {}".format(fsm.label))


def update(application: builder.DockerBuilder, reason: str, selected: str, version: Optional[str]):
    print("== Step 0: scanning {d}".format(d=application.root))
    application.scan()
    to_update = application.images_to_update()
    print("Will update the following images: ")
    for fsm in to_update:
        print("* {image}".format(image=fsm.image.name))
    print("== Step 1: adding updates")
    application.update_images(to_update, reason, selected, version=version)
