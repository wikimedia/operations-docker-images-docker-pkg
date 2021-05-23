import pytest

from docker_pkg import dockerfile


def test_dockerfile_has_numeric_user():
    assert dockerfile.has_numeric_user("USER 0\nUSER notmuchnumeric") is False
    assert dockerfile.has_numeric_user("RUN but\nNo user at all")
    assert dockerfile.has_numeric_user("USER root\nPrivileged\nUSER 123:12")
    assert dockerfile.has_numeric_user("USER 1000")
