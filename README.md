Docker-pkg
==========

`Docker-pkg` is a tool that allows writing and managing docker images in a
unified way, providing out of the box some useful functions:

* Dependency tracking between images (so that an image can automatically depend
  on the latest version of its parent image)
* Cache-avoiding out of the box
* Support for automatically use build images for creating artifacts
* Changelog based versioning
* Templating of common operations like installing packages, or image references

Overview
--------

`docker-pkg` accepts one mandatory parameter, the directory to scan for
docker images:

  $ docker-pkg <directory>

and will then scan that directory recursively for directories containing:

* A Dockerfile.template file
* A debian-formatted changelog

Based on information in the changelog, on configuration options, and on what is
found in the Dockerfile.template, a dockerfile is generated and an image is
built out of it. Name and tag of this image will be derived by the last entry in
the changelog.

Finally, the built images will be pushed to the configured registry, when
authentication credentials are provided.

Configuration
-------------

You can provide any variable via a configuration file (in YAML format) that you
will then be able to reference in your templates. In addition to those, there
are some builtin configuration variables that will not only be usable in your
templates, but also affect how `docker-pkg` works:

* `registry`: The address of the docker registry to use for all docker-related
  operations, including checking the published state of the image. The default
  value is None, and the default docker registry (that is, dockerhub) will be
  used.
* `namespace`: The namespace under which all the images to build should live.
* `username` and `password`: if set, they allow publishing the images you built
  to a remote repository.
* `http_proxy`: will set the http proxy to be used for all docker-related operations.

Build the images
----------------

This is as simple as launching `docker-pkg`, indicating as a first argument
the directory to scan for dockerfiles.

The build script will search the directory you provided it for Docker image
templates, which will be identified by four files:

* `Dockerfile.template` which is the template for the final Dockerfile
* `changelog` which is the changelog for the image and adheres to the Debian
  changelog format
* `control` (optional) which has the format of a Debian control file, and can be
  used for pointing out dependencies, build-dependencies, and add any other
  metadata that could be useful
* `Dockerfile.build.template` (optional) a dockerfile template for a build
  container (see below)

The name and tag of the image will be determined from the changelog file, so
it's mandatory that you add your changelog entry there. For most containers, a
debian-like versioning is a good idea to keep into account the security updates
that might happen.

The templating system
---------------------

Instead of writing plain dockerfiles, we think using jinja2 templates gives us
an edge: there are a lot of common constructs that we don't want to replicate,
and they are exposed to the templates as variables and filters:

### Variables

* `registry`: the address of the docker registry
* `seed_image`: the seed image to use as a base for the production dockerfiles


### Filters

* `image_tag`: This filter allows to retrieve the current image tag for a
  specific image name. This allows to keep all dependencies updated
  automagically in sync. Example

``` dockerfile
FROM {{ registry }}/{{ "nodejs-dev" | image_tag }}
# Will render to e.g. 'FROM my-registry/nodejs-dev:0.3.1'
```

 * `apt_install`: this filter will get the string you pass it as a list of
   packages to install with apt, and add the correct stanza to your
   dockerfile. It will also manage the setup of a proxy for apt if one is
   provided in the configuration via a `http_proxy` key

 * `apt_remove`: this filter will remove the packages listed in the string you
   pass to it, acting pretty much the same way `apt_install` does.

Build-stage containers
----------------------

`docker-pkg` allows using a build docker image to generate artifacts you
later want to use in the actual service container. Please note that when newer
docker versions including multi-stage builds are available, it might be
advisable to switch to that system.

If you need to build libraries or binaries but don't want to pollute your
container, you can create a `Dockerfile.build.template` in the container
directory, using the same syntax of the main docker container, and have the
build put any artifacts you'll want to use into the `/build` directory. That
directory will be later be copied to the `build` subdirectory of the main
Dockerfile build context, so you can use those.

Every build is performed in a temporary directory, and any leftovers of the
build (so the build image, any container spawned out of it, etc) will be taken
care of by the program.
