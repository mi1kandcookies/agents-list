"""Agent icons: one line icon per listing, picked by category.

The SVGs live in app/static/img/icons/ (Lucide, see CREDITS.md there). They
are inlined into the page so they take ``currentColor`` from the icon tile
and stay sharp at any pixel density.

Resolution order for an agent (an ``Agent`` row or its ``to_dict()``):
    1. ``agent.icon`` when it names an icon that exists (per-agent override,
       set by the demo seed for listings whose work is narrower than their
       category, e.g. SOC 2 readiness under Security)
    2. the icon for ``agent.category``
    3. ``FALLBACK``
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from markupsafe import Markup

ICON_DIR = Path(__file__).resolve().parent.parent / "static" / "img" / "icons"

# Every icon we ship, by key (= file name without .svg) and what it stands for.
ICONS: dict[str, str] = {
    "shield-check": "security and audits",
    "trending-up": "growth and marketing",
    "database": "data pipelines",
    "search": "research",
    "pen-line": "content and copy",
    "clipboard-check": "compliance",
    "circle-dollar-sign": "finance and investor updates",
    "plug": "API integrations",
    "workflow": "ops automation",
    "flask-conical": "QA and testing",
    "code-xml": "software development",
    "bot": "any other agent",
}

CATEGORY_ICONS: dict[str, str] = {
    "Security": "shield-check",
    "Marketing": "trending-up",
    "Data & Analytics": "database",
    "Research": "search",
    "Content": "pen-line",
    "Finance": "circle-dollar-sign",
    "Development": "code-xml",
    "Automation": "workflow",
}

FALLBACK = "bot"


def _get(agent, key: str):
    if isinstance(agent, dict):
        return agent.get(key)
    return getattr(agent, key, None)


def icon_for(agent) -> str:
    """The icon key for an agent row or agent dict."""
    override = _get(agent, "icon")
    if override in ICONS:
        return override
    return CATEGORY_ICONS.get(_get(agent, "category") or "", FALLBACK)


@lru_cache(maxsize=None)
def icon_svg(key: str) -> Markup:
    """The icon's SVG markup, ready to inline. Unknown keys get the fallback."""
    name = key if key in ICONS else FALLBACK
    svg = (ICON_DIR / f"{name}.svg").read_text(encoding="utf-8").strip()
    return Markup(svg.replace("<svg ", '<svg aria-hidden="true" focusable="false" ', 1))
