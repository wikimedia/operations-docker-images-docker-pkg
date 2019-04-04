docker-pkg changelog
^^^^^^^^^^^^^^^^^^^^

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
