"""Parsed-document shim over selectolax (04-audit-engine.md §8).

Checks receive a ``ParsedDocument`` rather than the raw parser, so the parser
implementation can be swapped without touching every check. selectolax (C
Modest/Lexbor engine) is 5-10x faster than BeautifulSoup on real HTML.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

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
    contexts: list[str]  # @context values; empty if absent or unparseable


class ParsedDocument:
    """Immutable after construction; the list accessors parse once and cache —
    the checks and the engine may each call them several times per audit.

    The accessors return the cached lists themselves, not copies: treat them
    as frozen. Mutating one (sort/pop/append) corrupts every later read of
    the same document within the audit.
    """

    def __init__(self, html: str, base_url: str) -> None:
        self._tree = HTMLParser(html)
        self._base = base_url
        self._headings: list[Heading] | None = None
        self._links: list[Link] | None = None
        self._images: list[Image] | None = None
        self._schema_blocks: list[SchemaBlock] | None = None

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
        if self._headings is None:
            out = []
            for node in self._tree.css("h1, h2, h3, h4, h5, h6"):
                out.append(Heading(level=int(node.tag[1]), text=node.text(strip=True)))
            self._headings = out
        return self._headings

    def links(self) -> list[Link]:
        """Anchors with an href, resolved to absolute.

        Hrefs the URL parser rejects outright (``http://[bad/y`` raises
        "Invalid IPv6 URL") are skipped rather than propagated. A href stdlib
        cannot parse is not a working link, and letting the ValueError escape
        makes one malformed anchor able to take down whatever is iterating —
        which is every caller, checks and engine alike.
        """
        if self._links is None:
            out = []
            for node in self._tree.css("a[href]"):
                href = node.attributes.get("href") or ""
                try:
                    resolved = urljoin(self._base, href)
                except ValueError:
                    continue
                out.append(
                    Link(
                        href=resolved,
                        text=node.text(strip=True),
                        rel=(node.attributes.get("rel") or ""),
                        target=(node.attributes.get("target") or ""),
                    )
                )
            self._links = out
        return self._links

    def images(self) -> list[Image]:
        if self._images is None:
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
            self._images = out
        return self._images

    def schema_blocks(self) -> list[SchemaBlock]:
        if self._schema_blocks is None:
            out = []
            for node in self._tree.css('script[type="application/ld+json"]'):
                raw = node.text() or ""
                try:
                    parsed: Any = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    out.append(SchemaBlock(raw=raw, types=[], valid_json=False, contexts=[]))
                    continue
                nodes = _schema_nodes(parsed)
                out.append(
                    SchemaBlock(
                        raw=raw,
                        types=_schema_types(nodes),
                        valid_json=True,
                        contexts=_schema_contexts(nodes),
                    )
                )
            self._schema_blocks = out
        return self._schema_blocks


def is_internal(href: str, host: str) -> bool:
    """Whether ``href`` targets ``host`` — THE internal/external boundary.

    Bare netloc equality, no www/port/case normalization. The internal-link
    count, the noopener check, and the engine's probe list must all apply the
    same test or one audit disagrees with itself about which links are
    internal — refine the boundary here or nowhere.
    """
    return urlparse(href).netloc == host


def is_external(href: str, host: str) -> bool:
    """Whether ``href`` leaves ``host`` for another one.

    Not the negation of ``is_internal``: hrefs with no netloc (mailto:, tel:,
    javascript:) are neither internal nor external.
    """
    return urlparse(href).netloc not in ("", host)


def _schema_contexts(nodes: list[dict[str, Any]]) -> list[str]:
    """Pull @context values out of a block's JSON-LD nodes.

    @context can be a bare string, a list, or an object mapping prefixes to
    vocabularies; only string forms are collected, which is what the
    schema.org-vocabulary check needs to assert.
    """
    contexts: list[str] = []
    for item in nodes:
        value = item.get("@context")
        if isinstance(value, str):
            contexts.append(value)
        elif isinstance(value, list):
            contexts.extend(v for v in value if isinstance(v, str))
        elif isinstance(value, dict):
            contexts.extend(v for v in value.values() if isinstance(v, str))
    return contexts


def _schema_types(nodes: list[dict[str, Any]]) -> list[str]:
    """Pull @type values out of a block's JSON-LD nodes."""
    types: list[str] = []
    for item in nodes:
        value = item.get("@type")
        if isinstance(value, str):
            types.append(value)
        elif isinstance(value, list):
            types.extend(str(v) for v in value)
    return types


def _schema_nodes(parsed: Any) -> list[dict[str, Any]]:
    """Every JSON-LD node in a block: top-level objects plus any ``@graph``
    members, one level of nesting deep (which is the shape in the wild).

    ``@graph`` is not an edge case — it is what Yoast, RankMath and most
    WordPress SEO plugins emit, so a reader that only looks at the top level
    sees no ``@type`` on a large share of real pages and reports valid
    structured data as broken.
    """
    items = parsed if isinstance(parsed, list) else [parsed]
    nodes: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        nodes.append(item)
        graph = item.get("@graph")
        if isinstance(graph, list):
            nodes.extend(node for node in graph if isinstance(node, dict))
        elif isinstance(graph, dict):
            nodes.append(graph)
    return nodes
