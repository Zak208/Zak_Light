"""Static checks of the hand-written window: it has no compiler, so typos would only show at run time."""
import os
import re

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "web")


def read(*parts):
    with open(os.path.join(WEB, *parts), encoding="utf-8") as f:
        return f.read()


SCRIPTS = re.findall(r'<script src="/static/([^"]+)"', read("index.html"))   # the window is several classic scripts
HTML, CSS = read("index.html"), read("static", "app.css")
JS = "\n".join(read("static", s) for s in SCRIPTS)
HTML_NO_COMMENTS = re.sub(r"<!--.*?-->", "", HTML, flags=re.S)


def test_everything_the_page_loads_exists_and_is_local():
    assert not re.findall(r"""(?:src|href)\s*=\s*["']https?://""", HTML)       # nothing from the internet
    assert "https://" not in JS and "http://" not in JS.replace("http://127.0.0.1", "")
    for ref in re.findall(r"""(?:src|href)\s*=\s*["']/static/([^"']+)""", HTML):
        assert os.path.isfile(os.path.join(WEB, "static", ref)), ref


def test_every_id_the_script_uses_exists_in_the_page():
    ids_in_html = set(re.findall(r'\sid="([\w-]+)"', HTML))
    used = set(re.findall(r"\$\('#([\w-]+)'\)", JS))
    used |= set(re.findall(r"slider\('([\w-]+)'", JS))
    used |= set(re.findall(r"sliders\['([\w-]+)'\]", JS))
    used |= set(re.findall(r"getElementById\('([\w-]+)'\)", JS))
    missing = sorted(i for i in used if i not in ids_in_html)
    assert not missing, f"ids used by app.js but missing from index.html: {missing}"


def test_every_icon_used_is_defined_in_the_sprite():
    defined = set(re.findall(r'<symbol id="([\w-]+)"', HTML))
    used = set(re.findall(r'href="#([\w-]+)"', HTML_NO_COMMENTS))
    assert used <= defined, used - defined


def test_every_api_path_the_script_calls_exists():
    paths = set(re.findall(r"""['"](/api/[\w/]+)['"]""", JS))
    assert paths, "the script should talk to the API"
    api_source = open(os.path.join(os.path.dirname(WEB), "api.py"), encoding="utf-8").read()
    for p in paths:  # routes are declared with decorators in api.py
        assert f'"{p}"' in api_source, f"{p} is called by app.js but not defined in api.py"


def test_no_framework_leftovers():
    assert "tailwind" not in HTML.lower() and "fa-solid" not in HTML
    assert not os.path.exists(os.path.join(WEB, "static", "tailwind.js"))
