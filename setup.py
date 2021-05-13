#!/usr/bin/env python
"""Package configuration."""

import sys
from setuptools import find_packages, setup

if sys.version_info < (3, 7):
    sys.exit("docker-pkg requires Python 3.7 or later")

long_description = """
docker-pkg-images builds docker images from templates.
"""

install_requires = [
    "docker >=5.0.0, <6.0.0",
    "pyyaml>=3.11",
    "jinja2>=2.9.6",
    "python-debian>=0.1.30",
    "requests",
]
test_requires = ["coverage", "pytest"]
extras = {
    "tests": test_requires,
    "doc": ["Sphinx"],
}
setup(
    author="Giuseppe Lavagetto",
    author_email="joe@wikimedia.org",
    classifiers=[
        "Environment :: Console",
        "Intended Audience :: System Administrators",
        "License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)",
        "Operating System :: MacOS :: MacOS X",
        "Operating System :: POSIX :: BSD",
        "Operating System :: POSIX :: Linux",
        "Programming Language :: Python :: 3 :: Only",
        "Topic :: System :: Systems Administration",
    ],
    description="Build docker images programmatically from templates.",
    entry_points={
        "console_scripts": [
            "docker-pkg = docker_pkg.cli:main",
        ],
    },
    install_requires=install_requires,
    tests_require=test_requires,
    extras_require=extras,
    license="GPLv3+",
    long_description=long_description,
    name="docker_pkg",
    packages=find_packages(exclude=["*.tests", "*.tests.*"]),
    platforms=["GNU/Linux", "BSD", "MacOSX"],
    version="3.0.3",
    url="https://github.com/wikimedia/operations-docker-images-docker-pkg/",
    zip_safe=False,
)
