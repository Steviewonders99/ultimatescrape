"""Extraction tests. These pin the behaviours that silently corrupt data when
they break — the failure mode for all of them is plausible-looking wrong output
rather than an exception."""

from __future__ import annotations

from ultimatescrape.fetch.extract import extract


def test_markdown_preserves_structure():
    html = """<html><body><main>
      <h2>Section</h2>
      <p>Hello <b>world</b>.</p>
      <ul><li>alpha</li><li>beta</li></ul>
      <a href="/docs">Docs</a>
    </main></body></html>"""
    doc = extract(html, "https://example.com/page")
    assert "## Section" in doc.markdown
    assert "- alpha" in doc.markdown and "- beta" in doc.markdown
    # Relative hrefs must be resolved, or downstream URL validation checks
    # nothing and every link "passes".
    assert "[Docs](https://example.com/docs)" in doc.markdown


def test_boilerplate_is_dropped():
    html = """<html><body>
      <nav>Home About Contact</nav>
      <script>var tracking = 1;</script>
      <main><p>The actual article body goes here.</p></main>
      <footer>Copyright 2026</footer>
    </body></html>"""
    doc = extract(html, "https://example.com/")
    assert "actual article body" in doc.markdown
    assert "tracking" not in doc.markdown
    assert "Copyright" not in doc.markdown


def test_jsonld_articlebody_wins():
    body = "Real article text. " * 30
    html = f"""<html><body>
      <script type="application/ld+json">
        {{"@type":"NewsArticle","articleBody":"{body}"}}
      </script>
      <main><p>navigation shell</p></main>
    </body></html>"""
    doc = extract(html, "https://example.com/")
    assert doc.strategy == "jsonld"
    assert "Real article text" in doc.markdown


def test_metadata_and_links():
    html = """<html lang="en-GB"><head>
      <meta property="og:title" content="The Title">
      <meta name="description" content="A description.">
      <meta property="article:published_time" content="2026-01-15T10:00:00Z">
      <link rel="canonical" href="https://example.com/canonical">
    </head><body><main><p>Body text here for the extractor to find.</p>
      <a href="https://other.example/x">Out</a>
      <a href="mailto:a@b.c">Mail</a>
      <a href="#frag">Frag</a>
    </main></body></html>"""
    doc = extract(html, "https://example.com/p")
    assert doc.title == "The Title"
    assert doc.description == "A description."
    assert doc.published_at.startswith("2026-01-15")
    assert doc.canonical == "https://example.com/canonical"
    # mailto and pure fragments are not crawlable and must not enter the frontier.
    assert "https://other.example/x" in doc.links
    assert not any(link.startswith("mailto:") for link in doc.links)


def test_truncation_is_marked():
    html = "<html><body><main><p>" + ("word " * 20000) + "</p></main></body></html>"
    doc = extract(html, "https://example.com/", max_chars=1000)
    assert len(doc.markdown) < 1200
    assert "truncated" in doc.markdown


def test_empty_and_malformed_html_do_not_raise():
    for html in ("", "<html>", "<<<>>>", "<html><body></body></html>"):
        doc = extract(html, "https://example.com/")
        assert doc.word_count >= 0
