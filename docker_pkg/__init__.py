"""
Generic helpers
"""

import logging
import os

log = logging.getLogger(__name__)


class ImageLabel:
    def __init__(self, config, name: str, version: str):
        self.namespace = config.get("namespace", "")
        self.registry = config.get("registry", "")
        self.short_name = name
        self.version = version

    def label(self, spec: str = "name") -> str:
        if spec == "short":
            return self.short_name
        elif spec == "name":
            return self._fn()
        elif spec == "full":
            return f"{self._fn()}:{self.version}"
        raise ValueError("Only 'short', 'name' and 'full' labels are supported.")

    def _fn(self):
        return os.path.join(self.registry, self.namespace, self.short_name)

    # Utility methods
    def name(self) -> str:
        """
        Canonical image name based on configured namespace and registry.
        """
        return self.label()

    def image(self) -> str:
        """
        Full label of the image including version, as used when pulling
        """
        return self.label("full")
