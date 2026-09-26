"""docs/site: every local link and anchor resolves, and every page shares
the stylesheet and navigation."""
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest

SITE = Path(__file__).resolve().parent.parent / "docs" / "site"
PAGES = sorted(SITE.glob("*.html"))


class _Collect(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id"):
            self.ids.add(attrs["id"])
        for key in ("href", "src"):
            if attrs.get(key):
                self.links.append(attrs[key])


def _parse(path: Path) -> _Collect:
    parser = _Collect()
    parser.feed(path.read_text(encoding="utf-8"))
    return parser


def test_site_has_pages():
    names = {p.name for p in PAGES}
    assert {"index.html", "getting-started.html", "api.html", "faq.html"} <= names
    assert (SITE / "site.css").is_file()


def test_site_uses_the_same_institutional_type_system():
    css = (SITE / "site.css").read_text(encoding="utf-8")
    assert "family=IBM+Plex+Mono" in css
    assert "family=Inter" in css
    assert "--font-body: 'Inter'" in css
    assert "--font-mono: 'IBM Plex Mono'" in css


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_local_links_resolve(page):
    parsed = _parse(page)
    broken = []
    for link in parsed.links:
        parts = urlsplit(link)
        if parts.scheme or link.startswith("//"):
            assert parts.scheme in ("https", "mailto"), f"{page.name}: insecure link {link}"
            continue
        target = (page.parent / parts.path).resolve() if parts.path else page
        if not target.is_file() or SITE.resolve() not in target.parents:
            broken.append(link)
            continue
        if parts.fragment and target.suffix == ".html" and parts.fragment not in _parse(target).ids:
            broken.append(link)
    assert not broken, f"{page.name}: {broken}"


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_every_page_links_every_other_page(page):
    links = {urlsplit(l).path for l in _parse(page).links}
    assert "site.css" in links
    assert {p.name for p in PAGES} <= links
