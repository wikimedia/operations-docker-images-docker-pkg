"""
Dockerfile.template processing

The files are Jinja2 templates, the class provides built-in templates to ease
writing Dockerfiles.
"""
import re

from typing import Any, Dict, Set

from jinja2 import Environment, FileSystemLoader, Template

from docker_pkg import log, ImageLabel


class TemplateEngine:
    known_images: Set[str] = set()
    config: Dict[str, Any] = {}
    env: Environment = Environment(extensions=["jinja2.ext.do"])

    @classmethod
    def setup(cls, config: Dict[str, Any], known_images: Set[str]):
        cls.config = config
        cls.known_images = known_images
        cls.setup_filters()

    @classmethod
    def setup_filters(cls):
        cls.setup_apt_install()
        cls.setup_apt_remove()

        def find_image_tag(image_name):
            label = ImageLabel(cls.config, image_name, "")
            image_name = label.label()
            for img_with_tag in cls.known_images:
                name, tag = img_with_tag.split(":")
                if image_name == name:
                    return img_with_tag
            raise ValueError("Image {name} not found".format(name=image_name))

        cls.env.filters["image_tag"] = find_image_tag

        def get_uid(user: str):
            mappings = cls.config["known_uid_mappings"]
            if user not in mappings:
                # If there is no available mapping, we just return the username.
                # If strict use of numeric uids is required by toggling the force_numeric_user
                # configuration option on, the dockerfile we generate will be rejected by
                # the check in has_numeric_user() at build time, thus the build
                # will fail.
                log.warn("UID mapping for user %s not found", user)
                return user
            else:
                return str(mappings[user])

        cls.env.filters["uid"] = get_uid

        def add_user(usr):
            id = get_uid(usr)
            if id == usr:
                raise ValueError("No mapping found for user '{u}'".format(u=usr))
            groupadd = "groupadd -o -g {id} -r {usr}".format(id=id, usr=usr)
            useradd = "useradd -l -o -r -m -d /var/lib/{usr} -g {usr} -u {id} {usr}".format(
                id=id, usr=usr
            )
            return "{g} && {u}".format(g=groupadd, u=useradd)

        cls.env.filters["add_user"] = add_user

    @classmethod
    def setup_apt_install(cls):
        t = Template(
            """
{%- if apt_only_proxy -%}
echo 'Acquire::http::Proxy \"{{ apt_only_proxy }}\";' > /etc/apt/apt.conf.d/80_proxy \\
    && apt-get update {{ apt_options }} \\
{%- else -%}
apt-get update {{ apt_options }} \\
{%- endif %}
    && DEBIAN_FRONTEND=noninteractive \\
    apt-get install {{ apt_options }} --yes {{ packages }} --no-install-recommends \\
{%- if apt_only_proxy %}
    && rm -f /etc/apt/apt.conf.d/80_proxy \\
{%- endif %}
    && apt-get clean && rm -rf /var/lib/apt/lists/* """
        )

        def apt_install(pkgs):
            # Allow people to write easier to read newline separated package
            # lists by turning them into space separated ones for apt
            pkgs = pkgs.replace("\n", " ")
            return t.render(packages=pkgs, **cls.config)

        cls.env.filters["apt_install"] = apt_install

    @classmethod
    def setup_apt_remove(cls):
        t = Template(
            """
{%- if apt_only_proxy -%}
echo 'Acquire::http::Proxy \"{{ apt_only_proxy }}\";' > /etc/apt/apt.conf.d/80_proxy  && \\
{%- endif -%}
    apt-get update && DEBIAN_FRONTEND=noninteractive apt-get remove --yes --purge {{ packages }} \\
{%- if apt_only_proxy %}
    && rm -f /etc/apt/apt.conf.d/80_proxy \\
{%- endif %}
    && apt-get clean && rm -rf /var/lib/apt/lists/* """
        )

        def apt_remove(pkgs):
            return t.render(packages=pkgs, **cls.config)

        cls.env.filters["apt_remove"] = apt_remove

    def __init__(self, path: str):
        self.env.loader = FileSystemLoader(path)


def from_template(path: str, name: str) -> Template:
    return TemplateEngine(path).env.get_template(name)


def has_numeric_user(dockerfile: str) -> bool:
    # Return true in case dockerfile does not contain a USER instruction
    numeric_user = True
    regex = re.compile(r"^USER\s+\d+(?:\:\d+)?$")
    for line in dockerfile.split("\n"):
        if line.startswith("USER "):
            numeric_user = True if regex.match(line) else False
    return numeric_user
