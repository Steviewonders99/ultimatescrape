"""HTML → clean markdown + metadata.

The extraction ladder is JSON-LD
``articleBody`` → ``<article>`` → ``<main>`` → ``<body>``), but the tag handling
combines that with a tag-aware text pass: it maps block
elements to newlines and list items to bullets instead of collapsing everything
into one line. Neither of those produced markdown; this does, because markdown
survives an LLM context far better than a wall of text — headings and lists give
the model structure to cite against.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser, Node

_DROP_TAGS = (
    "script",
    "style",
    "noscript",
    "nav",
    "footer",
    "header",
    "aside",
    "iframe",
    "svg",
    "form",
    "button",
)

_BOILERPLATE_HINTS = re.compile(
    r"(cookie|consent|subscribe|newsletter|advertisement|sidebar|related-posts|"
    r"social-share|breadcrumb|site-nav|menu-toggle)",
    re.IGNORECASE,
)


@dataclass
class Extracted:
    url: str
    title: str | None = None
    markdown: str = ""
    text: str = ""
    description: str | None = None
    byline: str | None = None
    published_at: str | None = None
    lang: str | None = None
    canonical: str | None = None
    links: list[str] = field(default_factory=list)
    jsonld: list[dict] = field(default_factory=list)
    word_count: int = 0
    strategy: str = "none"

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["links"] = self.links[:200]
        return d


def _meta(tree: HTMLParser, *selectors: str) -> str | None:
    for sel in selectors:
        node = tree.css_first(sel)
        if node:
            val = (node.attributes.get("content") or node.text() or "").strip()
            if val:
                return val
    return None


def _collect_jsonld(tree: HTMLParser) -> list[dict]:
    out: list[dict] = []
    for node in tree.css('script[type="application/ld+json"]'):
        raw = node.text(strip=True)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items = parsed if isinstance(parsed, list) else [parsed]
        for item in items:
            if isinstance(item, dict):
                out.append(item)
                # @graph is how most CMS plugins nest the real entities.
                graph = item.get("@graph")
                if isinstance(graph, list):
                    out.extend(g for g in graph if isinstance(g, dict))
    return out


def _jsonld_article_body(blocks: list[dict]) -> str | None:
    for block in blocks:
        body = block.get("articleBody")
        if isinstance(body, str) and len(body) > 200:
            return body
    return None


def _is_boilerplate(node: Node) -> bool:
    attrs = node.attributes
    blob = f"{attrs.get('class') or ''} {attrs.get('id') or ''} {attrs.get('role') or ''}"
    return bool(_BOILERPLATE_HINTS.search(blob))


def _to_markdown(node: Node, base_url: str) -> str:
    """Render a subtree as markdown. Deliberately small — headings, lists,
    links, code, blockquotes, tables-as-text. Everything else becomes a
    paragraph."""
    parts: list[str] = []

    def walk(n: Node, in_list: bool = False) -> None:
        tag = n.tag
        if tag in _DROP_TAGS or (tag != "-text" and _is_boilerplate(n)):
            return
        if tag == "-text":
            txt = n.text(strip=False)
            if txt and txt.strip():
                parts.append(txt)
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            parts.append(f"\n\n{'#' * level} {n.text(strip=True)}\n\n")
            return
        if tag == "li":
            parts.append("\n- " + n.text(strip=True))
            return
        if tag == "br":
            parts.append("\n")
            return
        if tag == "hr":
            parts.append("\n\n---\n\n")
            return
        if tag in ("pre", "code") and tag == "pre":
            parts.append(f"\n\n```\n{n.text(strip=False).strip()}\n```\n\n")
            return
        if tag == "blockquote":
            body = n.text(strip=True)
            parts.append("\n\n" + "\n".join(f"> {ln}" for ln in body.splitlines() if ln) + "\n\n")
            return
        if tag == "a":
            href = n.attributes.get("href") or ""
            label = n.text(strip=True)
            if label and href and not href.startswith(("javascript:", "#")):
                parts.append(f"[{label}]({urljoin(base_url, href)})")
                return
        if tag == "img":
            alt = (n.attributes.get("alt") or "").strip()
            if alt:
                parts.append(f"![{alt}]()")
            return
        if tag in ("p", "div", "section", "article", "tr", "table", "ul", "ol", "main"):
            parts.append("\n\n")

        for child in n.iter(include_text=True):
            walk(child, in_list=in_list or tag in ("ul", "ol"))

        if tag in ("p", "div", "section", "article", "tr", "table", "ul", "ol", "main"):
            parts.append("\n\n")

    walk(node)
    md = "".join(parts)
    md = re.sub(r"[ \t]+", " ", md)
    md = re.sub(r" *\n *", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def extract(html: str, url: str, *, max_chars: int = 40_000) -> Extracted:
    tree = HTMLParser(html)

    # JSON-LD must be harvested BEFORE boilerplate removal: it lives inside
    # <script type="application/ld+json">, and dropping scripts first silently
    # disables the highest-quality extraction path entirely.
    jsonld = _collect_jsonld(tree)

    for tag in _DROP_TAGS:
        for node in tree.css(tag):
            node.decompose()

    out = Extracted(url=url, jsonld=jsonld)

    html_node = tree.css_first("html")
    out.lang = (html_node.attributes.get("lang") if html_node else None) or None
    out.title = _meta(tree, 'meta[property="og:title"]', 'meta[name="twitter:title"]', "title")
    out.description = _meta(
        tree, 'meta[property="og:description"]', 'meta[name="description"]'
    )
    out.byline = _meta(tree, 'meta[property="article:author"]', 'meta[name="author"]')
    out.published_at = _meta(
        tree,
        'meta[property="article:published_time"]',
        'meta[name="publish-date"]',
        'meta[itemprop="datePublished"]',
        "time[datetime]",
    )
    canonical = tree.css_first('link[rel="canonical"]')
    out.canonical = canonical.attributes.get("href") if canonical else None

    seen: set[str] = set()
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urljoin(url, href)
        if urlparse(absolute).scheme in ("http", "https") and absolute not in seen:
            seen.add(absolute)
            out.links.append(absolute)

    body = _jsonld_article_body(jsonld)
    if body:
        out.markdown = body.strip()
        out.strategy = "jsonld"
    else:
        for selector, name in (
            ("article", "article"),
            ("main", "main"),
            ('[role="main"]', "role-main"),
            ("body", "body"),
        ):
            node = tree.css_first(selector)
            if node:
                rendered = _to_markdown(node, url)
                # A nav-only <article> shell is worse than falling through.
                if len(rendered) > 200 or name == "body":
                    out.markdown = rendered
                    out.strategy = name
                    break

    if len(out.markdown) > max_chars:
        out.markdown = out.markdown[:max_chars].rstrip() + "\n\n…[truncated]"

    out.text = re.sub(r"[#>*`\-\[\]()]", " ", out.markdown)
    out.text = re.sub(r"\s+", " ", out.text).strip()
    out.word_count = len(out.text.split())
    return out


def now_iso() -> str:
    return datetime.now(UTC).isoformat()
