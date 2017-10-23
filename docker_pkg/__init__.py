import logging

log = logging.getLogger(__name__)


def image_fullname(name, config):
    namespace = config.get('namespace', False)
    reg = config.get('registry', False)
    if namespace:
        name = '{namespace}/{name}'.format(namespace=namespace, name=name)
    if reg:
        name = '{registry}/{name}'.format(registry=reg, name=name)
    return name
