docker-pkg changelog
^^^^^^^^^^^^^^^^^^^^
`v4.0.1` (2024-06-14)
^^^^^^^^^^^^^^^^^^^^^
Bugfixes:
"""""""""
* Call test commands with the full image name (including) tag, rather than just the image name

`v4.0.0` (2023-11-02)
^^^^^^^^^^^^^^^^^^^^^
API breaking changes
""""""""""""""""""""
* Removed the concept of a `seed_image`, now superseded by multiple base images

New features
""""""""""""
* New driver interface that allows to add multiple build backends
* docker-pkg will now search for an user provided configuration file under "~/.config/docker-pkg.yaml"
* PATH is injected in the test script as it's a common mistake
* Undefined variables in templates now cause a build failure
* Added validation with mypy, python 3.11

Bugfixes:
"""""""""
* Pin urllib3 to < 2.x
* remove ms from datefmt
* Fix sphinx deprecations

`v3.0.3` (2021-05-13)
^^^^^^^^^^^^^^^^^^^^^
Bugfixes:
"""""""""
* Use the full image tag when pulling from a registry
* Bump docker-py dependency to the latest major

`v3.0.2` (2021-02-16)
^^^^^^^^^^^^^^^^^^^^^

New features
""""""""""""
Add a new filter called add_user to help adding users that have a uid mapping.

Bugfixes:
"""""""""
Use the set HTTP proxy when running verification steps

`v3.0.1` (2021-02-05)
^^^^^^^^^^^^^^^^^^^^^

Bugfixes:
"""""""""
Actually use the builder.DockerBuilder methods when building images.

`v3.0.0`_ (2021-02-05)
^^^^^^^^^^^^^^^^^^^^^^

API breaking changes
""""""""""""""""""""
* Removed the build image functionality as it is unused since docker supports multi-stage builds natively.


New features
""""""""""""
* Added a new template function 'uid' that allows determining the UID of some well known users (specified in the "known_uid_mappings" config key).
* Added the option to enforce images to be build with a numeric UID in the last USER instruction. This can be toggled via the "force_numeric_user" config key.
* A verify step was introduced that allows users to run tests on images before publishing them. By default the script "test.sh" (if it exists) is executed with the image name as argument. May be overwritten via "verify_command" and "verify_args".


`v2.0.0`_ (2019-04-09)
^^^^^^^^^^^^^^^^^^^^^^

API breaking changes
""""""""""""""""""""

* An action must be specified at all times, ``build`` is not the implicit behaviour anymore

* The ``--nightly`` switch now works again by labelling the day, not
  the minute. For that functionality a ``--snapshot`` has been introduced


New features
""""""""""""
* New action ``update`` allows to indicate an image you want to update
  and to create the corresponding changelog entries for all the
  children / dependent images

* New ``--snapshot`` build switch designed to be used by developers in
  testing.


Bug Fixes
"""""""""

* Prune action fixes:
  * Prune uses an inverted build chain so that we shouldn't meet
    issues with dependent images
  * Prune now correctly handles the case where a nightly build needs
    to be preserved.
  * Prune respects the selections we make
