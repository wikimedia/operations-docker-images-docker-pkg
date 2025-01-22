Docker-pkg
==========

``Docker-pkg`` is a tool that allows writing and managing docker images in a
unified way, providing out of the box some useful functions:

* Dependency tracking between images (so that an image can automatically depend
  on the latest version of its parent image)
* Cache-avoiding out of the box
* Support for automatically use build images for creating artifacts
* Changelog based versioning (with nightly builds support)
* Templating of common operations like installing packages, or image references
* Helpers for build environment maintenance: simple purging of images
  and a utility to recursively update changelogs

Overview
--------

``docker-pkg`` can perform three fundamental actions:

* ``build`` [BUILD_OPTS] <directory> which builds the images in the directory
* ``prune`` [PRUNE_OPTS] <directory> which prunes old images from the
  local daemon
* ``update`` [UPDATE_OPTS] <name> <directory> will create a changelog
  entry for <name> and all its dependent images so that they will be
  rebuilt with the next build.

As can be seen, in all cases we provide ``docker-pkg`` with a
directory where our image definitions are located.  ``docker-pkg``
will scan that directory recursively for directories containing:

* A ``Dockerfile.template`` file
* A debian-formatted ``changelog``

Based on information in the changelog, on configuration options, and on what is
found in the Dockerfile.template, a Dockerfile is generated.

If you chose to build images, that dockerfile (and the content of the
directory) are used to build an image. The name and tag of an image will
be derived by the last entry its changelog.
Finally, the built images will be pushed to the configured registry, when
authentication credentials are provided.

Configuration
-------------

You can provide any variable via a configuration file (in YAML format) that you
will then be able to reference in your templates.

A user configuration file `$XDG_CONFIG_HOME/docker-pkg.yaml` or
`~/.config/docker-pkg.yaml` will be loaded first when it exists. It can be used
to share common settings between multiple docker-pkg definition repositories
(for example `apt_only_proxy`). Settings defined in `--configfile` take
precedence over user settings.

In addition to those, there are some builtin configuration variables that will
not only be usable in your templates, but also affect how ``docker-pkg`` works:

* ``registry``: The address of the docker registry to use for all docker-related
  operations, including checking the published state of the image. The default
  value is None, and the default docker registry (that is, dockerhub) will be
  used.
* ``namespace``: The namespace under which all the images to build should live.
* ``username`` and ``password``: if set, they allow publishing the images you built
  to a remote repository.
* ``http_proxy``: will set the http proxy to be used for all docker-related operations.
* ``apt_only_proxy``: will set up an http proxy to be used exclusively by apt. Please 
  note that if you define `http_proxy` and not this variable, the value of `http_proxy`
  will be used as an apt proxy as well.
* ``apt_options``: a string containing additional apt options you want to add to the apt 
  command line when installing packages. 
* ``base_images``: images that are not built by docker-pkg but should be present
  or pulled in order to be able to build the images.
* ``scan_workers``: maximum number of threads to use when scanning local
  definition of images. For each image found, ``docker-pkg`` queries the local
  Docker daemon and the registry. Default: 8.
* ``known_uid_mappings`` is a dictionary of username:uid mappings that can be used with the
  `uid` template helper.
* ``container_limits`` apply cpu/memory constraints to the builds. The available settings are:
  `memory` (int): set memory limit for build, `memswap` (int): Total memory (memory + swap, -1 to disable),
  `cpushares` (int): CPU shares (relative weight) and `cpusetcpus` (str): CPUs in which to allow execution (e.g.,
  "0-3", "0,1"). These settings can be expressed as a comma separated list, like: `cpusetcpus=0-2,memory=4000000`.
* `verify_command` and `verify_args` specify which command to run, with which arguments, to verify 
  that an image that was built actually works as intended. By default, the `test.sh` file in the 
  image directory will be run, with the image full name as argument. If such file is not found, no verification step proceeds.
  You can switch to another testing framework, for example by using pytest as follows:

.. code-block:: YAML

    verify_command: pytest
    # This assumes your tests will accept a `--image` parameter.
    verify_args: ["{path}/test_image.py", "--image", "{image}"]

Build the images
----------------

This is as simple as launching ``docker-pkg build``, indicating as next argument
the directory to scan for dockerfiles.

The build script will search the directory you provided it for Docker image
templates, which will be identified by four files:

* ``Dockerfile.template`` which is the template for the final Dockerfile
* ``changelog`` which is the changelog for the image and adheres to the Debian
  changelog format
* ``control`` (optional) which has the format of a Debian control file, and can be
  used for pointing out dependencies, build-dependencies, and add any other
  metadata that could be useful
* ``Dockerfile.build.template`` (optional) a dockerfile template for a build
  container (see below)

The name and tag of the image will be determined from the changelog file, so
it's mandatory that you add your changelog entry there. For most containers, a
debian-like versioning is a good idea to keep into account the security updates
that might happen.

In case you run ``docker-pkg build`` with the command-line switch
``--nightly``, a nightly build will be performed, appending the
current date and time to the tag defined in the changelog of each image, and
thus triggering a rebuild of all images.

The templating system
---------------------

Instead of writing plain dockerfiles, we think using jinja2 templates gives us
an edge: there are a lot of common constructs that we don't want to replicate,
and they are exposed to the templates as variables and filters:

Variables
'''''''''

* ``registry``: the address of the docker registry


Filters
'''''''

* ``image_tag``: This filter allows to retrieve the current image tag for a
  specific image name. This allows to keep all dependencies updated
  automagically in sync. Example:

 .. code-block:: dockerfile

    FROM {{ registry }}/{{ "nodejs-dev" | image_tag }}
    # Will render to e.g. 'FROM my-registry/nodejs-dev:0.3.1'

* ``upstream_version``: This filter takes the output of image_tag and returns
  the upstream version part of the tag, before any Debianized version suffix.
  Example:

 .. code-block:: dockerfile

    ARG UPSTREAM_VERSION={{ "jaeger" | image_tag | upstream_version }}
    RUN curl "https://${URL}/jaeger-${UPSTREAM_VERSION}-linux-amd64.tar.gz"

* ``apt_install``: this filter will get the string you pass it as a list of
  packages to install with apt, and add the correct stanza to your dockerfile.
  It will also manage the setup of a proxy for apt if one is provided in the
  configuration via a ``http_proxy`` key

* ``apt_remove``: this filter will remove the packages listed in the string you
  pass to it, acting pretty much the same way ``apt_install`` does.


* ``uid``: this filter will take a username as input, and output the corresponding
  UID if a corresponding mapping is saved in `known_uid_mappings` in the configuration.

* ``add_user``: this filter will take a username as input, and output the instructions
  to create an user with that username, as long as an uid mapping is provided in the
  configuration.

Build-stage artifacts
----------------------

Every build is performed in a temporary directory, and any leftovers of the
build (so the build image, any container spawned out of it, etc) will be taken
care of by the program.

Prune
-----
When you build images often, you'll end up with a sizable amount of
wasted disk space by hosting old image builds on your
system. ``docker-pkg prune`` will remove from the local docker daemon
all those images that are contained in `<directory>` at version
different than the most recent entry in the changelog file.

Update
------
It's pretty common we need to rebuild a base image and having to
rebuild all the images that depend upon it. ``docker-pkg update``
partially automates the process creating a changelog entry with a
pre-baked message for each of those images. this will trigger a
rebuild of those images next time ``docker-pkg build`` is launched.

.. code-block:: console

   $ docker-pkg update --reason 'CVE-XYZ isArrayish RCE' nodejs images
   # This will first check images that are not on the registry or
   # locally built and build/publish them
   $ docker-pkg build

Troubleshooting
---------------

When building images on macOS, you may see an error like this:

.. code-block:: console

   OSError: Could not find a suitable TLS CA certificate bundle, invalid path: /etc/ssl/certs/ca-certificates.crt

To work around this, open Keychain Access, navigate to System Roots ->
Certificates, select all certificates and go to File -> Export Items. Select
the export format as Certificate (.cer). Save the file to a temporary
location, then ``mv`` it to ``/etc/ssl/certs/ca-certificates.crt``.
