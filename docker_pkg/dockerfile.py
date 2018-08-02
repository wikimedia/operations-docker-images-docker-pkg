from jinja2 import Environment, FileSystemLoader, Template
from docker_pkg import image_fullname


class TemplateEngine(object):
    known_images = []
    config = {}
    env = None

    @classmethod
    def setup(cls, config, known_images):
        cls.env = Environment(extensions=['jinja2.ext.do'])
        cls.config = config
        cls.known_images = known_images
        cls.setup_filters()

    @classmethod
    def setup_filters(cls):
        cls.setup_apt_install()
        cls.setup_apt_remove()

        def find_image_tag(image_name):
            image_name = image_fullname(image_name, cls.config)

            for img_with_tag in cls.known_images:
                name, tag = img_with_tag.split(':')
                if image_name == name:
                    return img_with_tag
            raise ValueError("Image {name} not found".format(name=image_name))
        cls.env.filters['image_tag'] = find_image_tag

    @classmethod
    def setup_apt_install(cls):
        t = Template("""
{%- if http_proxy -%}
echo 'Acquire::http::Proxy \"{{ http_proxy }}\";' > /etc/apt/apt.conf.d/80_proxy \\
    && apt-get update {{ apt_options }} \\
{%- else -%}
apt-get update {{ apt_options }} \\
{%- endif %}
    && DEBIAN_FRONTEND=noninteractive \\
    apt-get install {{ apt_options }} --yes {{ packages }} --no-install-recommends \\
{%- if http_proxy %}
    && rm -f /etc/apt/apt.conf.d/80_proxy \\
{%- endif %}
    && apt-get clean && rm -rf /var/lib/apt/lists/* """)

        def apt_install(pkgs):
            # Allow people to write easier to read newline separated package
            # lists by turning them into space separated ones for apt
            pkgs = pkgs.replace('\n', ' ')
            return t.render(packages=pkgs, **cls.config)
        cls.env.filters['apt_install'] = apt_install

    @classmethod
    def setup_apt_remove(cls):
        t = Template("""
{%- if http_proxy -%}
echo 'Acquire::http::Proxy \"{{ http_proxy }}\";' > /etc/apt/apt.conf.d/80_proxy  && \\
{%- endif -%}
    apt-get update && DEBIAN_FRONTEND=noninteractive apt-get remove --yes --purge {{ packages }} \\
{%- if http_proxy %}
    && rm -f /etc/apt/apt.conf.d/80_proxy \\
{%- endif %}
    && apt-get clean && rm -rf /var/lib/apt/lists/* """)

        def apt_remove(pkgs):
            return t.render(packages=pkgs, **cls.config)
        cls.env.filters['apt_remove'] = apt_remove

    def __init__(self, path):
        self.env.loader = FileSystemLoader(path)


def from_template(path, name):
    return TemplateEngine(path).env.get_template(name)
