"""
Generic helpers
"""

import logging

log = logging.getLogger(__name__)


def image_fullname(name, config):
    """
    Canonical image name based on configured namespace and registry.

    :param str name: Base name of the Docker image.
    :param dict config: docker-pkg configuration:

        * ``registry``: Docker registry domain name (Default: False)
        * ``namespace``: Docker registry namespace (Default: False)

    :return: Canonical image name with namespace/registry if set in the config.

    """

    namespace = config.get('namespace', False)
    reg = config.get('registry', False)
    if namespace:
        name = '{namespace}/{name}'.format(namespace=namespace, name=name)
    if reg:
        name = '{registry}/{name}'.format(registry=reg, name=name)
    return name
