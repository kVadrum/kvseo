"""Parsed-document shim over selectolax (04-audit-engine.md §8).

Checks receive a ``ParsedDocument`` rather than the raw parser, so the parser
implementation can be swapped without touching every check. selectolax (C
Modest/Lexbor engine) is 5-10x faster than BeautifulSoup on real HTML.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from selectolax.parser import HTMLParser


@dataclass(frozen=True)
class Heading:
    level: int  # 1-6
    text: str


@dataclass(frozen=True)
class Link:
    href: str  # resolved to absolute
    text: str
    rel: str
    target: str


@dataclass(frozen=True)
class Image:
    src: str
    alt: str | None  # None = attribute absent; "" = present but empty
    width: str | None
    height: str | None


@dataclass(frozen=True)
class SchemaBlock:
    raw: str
    types: list[str]  # @type values; empty if unparseable
    valid_json: bool


class ParsedDocument:
    def __init__(self, html: str, base_url: str) -> None:
        self._tree = HTMLParser(html)
        self._base = base_url

    def title(self) -> str | None:
        node = self._tree.css_first("title")
        if node is None:
            return None
        return node.text(strip=True) or None

    def meta_name(self, name: str) -> str | None:
        for node in self._tree.css("meta"):
            if (node.attributes.get("name") or "").lower() == name.lower():
                return node.attributes.get("content")
        return None

    def meta_property(self, prop: str) -> str | None:
        for node in self._tree.css("meta"):
            if (node.attributes.get("property") or "").lower() == prop.lower():
                return node.attributes.get("content")
        return None

    def link_rel(self, rel: str) -> str | None:
        for node in self._tree.css("link"):
            if (node.attributes.get("rel") or "").lower() == rel.lower():
                href = node.attributes.get("href")
                return urljoin(self._base, href) if href else None
        return None

    def html_lang(self) -> str | None:
        node = self._tree.css_first("html")
        return node.attributes.get("lang") if node else None

    def headings(self) -> list[Heading]:
        # CSS selection returns nodes in document order — needed for hierarchy.
        out = []
        for node in self._tree.css("h1, h2, h3, h4, h5, h6"):
            out.append(Heading(level=int(node.tag[1]), text=node.text(strip=True)))
        return out

    def links(self) -> list[Link]:
        out = []
        for node in self._tree.css("a[href]"):
            href = node.attributes.get("href") or ""
            out.append(
                Link(
                    href=urljoin(self._base, href),
                    text=node.text(strip=True),
                    rel=(node.attributes.get("rel") or ""),
                    target=(node.attributes.get("target") or ""),
                )
            )
        return out

    def images(self) -> list[Image]:
        out = []
        for node in self._tree.css("img"):
            out.append(
                Image(
                    src=urljoin(self._base, node.attributes.get("src") or ""),
                    alt=node.attributes.get("alt"),
                    width=node.attributes.get("width"),
                    height=node.attributes.get("height"),
                )
            )
        return out

    def schema_blocks(self) -> list[SchemaBlock]:
        out = []
        for node in self._tree.css('script[type="application/ld+json"]'):
            raw = node.text() or ""
            try:
                parsed: Any = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                out.append(SchemaBlock(raw=raw, types=[], valid_json=False))
                continue
            out.append(SchemaBlock(raw=raw, types=_schema_types(parsed), valid_json=True))
        return out


def _schema_types(parsed: Any) -> list[str]:
    """Pull @type values out of a parsed JSON-LD block (object or list)."""
    items = parsed if isinstance(parsed, list) else [parsed]
    types: list[str] = []
    for item in items:
        if isinstance(item, dict):
            value = item.get("@type")
            if isinstance(value, str):
                types.append(value)
            elif isinstance(value, list):
                types.extend(str(v) for v in value)
    return types
