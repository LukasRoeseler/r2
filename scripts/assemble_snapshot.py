#!/usr/bin/env python3
"""Assemble the multi-file mirror in _site/ into one self-contained HTML file.

Run `BASE_URL=/ python scripts/build.py` first - this script reads that
already-built _site/ tree (root-relative links, one directory per page) and
flattens it into a single _site/r2-mirror-snapshot.html: every page becomes a
<section> with a path bar showing its original URL path (e.g. /articles/9577/)
so a specific page can be cited or linked to directly via a fragment
(#articles/9577), CSS and local images are inlined, and the shared
header/footer are kept exactly once. Meant for the "Build Zenodo snapshot
export" workflow, so a Zenodo-archived version needs no server and no
companion asset folder - just the one file.
"""

import base64
import datetime
import json
import mimetypes
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "_site")
DATA = os.path.join(ROOT, "data")

LIVE_BASE = "https://replicationresearch.github.io/"

MAIN_RE = re.compile(r'<main id="main">(.*)</main>', re.DOTALL)
HEADER_RE = re.compile(r'(<header class="site-header">.*?</header>)', re.DOTALL)
FOOTER_RE = re.compile(r'(<footer class="site-footer">.*?</footer>)', re.DOTALL)
PDF_TAB_RE = re.compile(
    r'<button class="view-tab is-active" type="button" role="tab"\s*'
    r'id="tab-pdf" aria-controls="panel-pdf" aria-selected="true">PDF</button>')
PDF_PANEL_RE = re.compile(
    r'<div class="view-panel is-active" role="tabpanel" id="panel-pdf" '
    r'aria-labelledby="tab-pdf">.*?</div>', re.DOTALL)
HTML_TAB_INACTIVE_RE = re.compile(
    r'<button class="view-tab" type="button" role="tab"\s*'
    r'id="tab-html" aria-controls="panel-html"\s*'
    r'aria-selected="false">')
HTML_PANEL_HIDDEN_RE = re.compile(
    r'<div class="view-panel" role="tabpanel"\s*'
    r'id="panel-html" aria-labelledby="tab-html" hidden>')
# Anchored on the (stable, distinctive) preceding goatcounter <script> tag
# rather than matching this script's own internal implementation details -
# the latter previously broke silently the moment that script's JS was
# refactored, since the regex no longer matched anything.
EXTERNAL_SCRIPT_RE = re.compile(
    r'<script data-goatcounter=.*?</script>\s*(<script>.*?</script>)', re.DOTALL)

# Every id="..." within one page's own content is only guaranteed unique
# within that standalone page, not across the many pages concatenated into
# the single-file snapshot (template-static ids like tab-html/panel-html/
# fulltext-settings, article-scoped-but-not-globally-scoped footnote ids
# like fn-2-1, and coincidentally-matching auto-generated heading ids from
# Quarto-authored guide content all collide this way) - namespace_page_ids()
# finds every id in a page's content and prefixes it, its HTML references
# (href="#...", aria-controls, aria-labelledby, for), and any
# getElementById('...') call in that same content's inline scripts.
ID_ATTR_RE = re.compile(r'\bid="([^"]+)"')
ID_REF_RE = re.compile(r'\b(href="#|aria-controls="|aria-labelledby="|for=")([^"]+)"')
GET_ELEMENT_BY_ID_RE = re.compile(r"getElementById\((['\"])([^'\"]+)\1\)")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def load_json(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as fh:
        return json.load(fh)


def page_path(*parts):
    """Anchor id / cited path for a page, e.g. page_path("articles", "9577")
    -> "articles/9577". No leading/trailing slash - the path bar and TOC add
    the slashes back for display.
    """
    return "/".join(p.strip("/") for p in parts if p)


def collect_pages():
    """(anchor, label, group, _site-relative index.html path) for every page,
    in reading order. Read from data/*.json rather than walked off disk so
    articles/issues sort newest-first like the live site, and dropping a page
    type here (or OJS adding a new one) doesn't require touching this list by
    hand for anything already covered by pages.json.
    """
    pages = []

    pages.append(("home", "Home", "Home", "index.html"))

    issues = sorted(load_json("issues.json"),
                     key=lambda i: i.get("datePublished") or "", reverse=True)
    pages.append((page_path("issues"), "All issues", "Issues",
                  os.path.join("issues", "index.html")))
    for issue in issues:
        pages.append((page_path("issues", issue["id"]), issue["title"],
                      "Issues", os.path.join("issues", issue["id"], "index.html")))

    articles = sorted(load_json("articles.json"),
                       key=lambda a: a.get("datePublished") or "", reverse=True)
    for a in articles:
        pages.append((page_path("articles", a["urlPath"]), a["title"],
                      "Articles",
                      os.path.join("articles", a["urlPath"], "index.html")))

    announcements = load_json("announcements.json")
    pages.append((page_path("announcements"), "All announcements",
                  "Announcements", os.path.join("announcements", "index.html")))
    for ann in announcements:
        pages.append((page_path("announcements", ann["id"]), ann["title"],
                      "Announcements",
                      os.path.join("announcements", ann["id"], "index.html")))

    for page in load_json("pages.json"):
        out = os.path.join(*page["slug"].split("/"), "index.html")
        pages.append((page_path(page["slug"]), page["title"], "Pages", out))

    return pages


def extract_main(html):
    match = MAIN_RE.search(html)
    if not match:
        raise RuntimeError("could not find <main id=\"main\"> block")
    return match.group(1)


def data_uri(local_path):
    """base64 data: URI for a file under _site/, or None if it's missing
    (e.g. an optional image that wasn't scraped for this run)."""
    full = os.path.join(SITE, local_path.lstrip("/"))
    if not os.path.isfile(full):
        return None
    mime = mimetypes.guess_type(full)[0] or "application/octet-stream"
    with open(full, "rb") as fh:
        encoded = base64.b64encode(fh.read()).decode("ascii")
    return "data:%s;base64,%s" % (mime, encoded)


def inline_images(html):
    def repl(match):
        src = match.group(1)
        if src.startswith("//") or not src.startswith("/"):
            return match.group(0)
        uri = data_uri(src)
        return 'src="%s"' % uri if uri else match.group(0)

    return re.sub(r'src="(/[^"]*)"', repl, html)


def rewrite_internal_hrefs(html):
    """/articles/9577/ -> #articles/9577; /#under-review -> #under-review
    (fragment already unique and still present); /articles/9577/#preview ->
    #articles/9577 (that target section was dropped, see strip_pdf_previews).
    """
    def repl(match):
        raw = match.group(1)
        path, _, fragment = raw.partition("#")
        anchor = page_path(path) or "home"
        if fragment and fragment != "preview":
            return 'href="#%s"' % fragment
        return 'href="#%s"' % anchor

    return re.sub(r'href="(/[^"]*)"', repl, html)


def strip_pdf_previews(html):
    """Drop the embedded pdf.js iframe per article - it loads a whole
    separate app plus the PDF over relative paths that don't exist once this
    is one offline file. The aside's Download PDF button (an absolute,
    external OJS URL) still gets people to the PDF. The PDF tab/panel share
    a <section> with the extracted HTML full-text tab, so only that inner
    panel is dropped, not the whole section - and if a PDF panel was in fact
    removed, the HTML tab (previously hidden behind it) becomes the default
    view instead of being hidden with nothing showing above it."""
    had_pdf_panel = PDF_PANEL_RE.search(html) is not None
    html = PDF_TAB_RE.sub("", html)
    html = PDF_PANEL_RE.sub("", html)
    if had_pdf_panel:
        html = HTML_TAB_INACTIVE_RE.sub(
            '<button class="view-tab is-active" type="button" role="tab" '
            'id="tab-html" aria-controls="panel-html" aria-selected="true">',
            html)
        html = HTML_PANEL_HIDDEN_RE.sub(
            '<div class="view-panel is-active" role="tabpanel" '
            'id="panel-html" aria-labelledby="tab-html">',
            html)
    return html


def namespace_page_ids(html, anchor):
    """Every page becomes one <section> among many concatenated into a
    single file, so any id="..." that's only unique within one standalone
    page (template-static ones like tab-html/panel-html/fulltext-settings,
    the article-views section's own id="preview", article-scoped footnote
    ids like fn-2-1, or two unrelated pages' Quarto-generated heading ids
    that happen to match) collides across pages here. Finds every id in
    this page's own content and prefixes it, its HTML references
    (href="#...", aria-controls, aria-labelledby, for), and any
    getElementById('...') call in this same content's inline scripts -
    the latter matters because at least one script (the reading-options
    panel's) looks its elements up by a hardcoded id string rather than by
    reading an aria-controls-style attribute back off the DOM, so renaming
    the id without also updating that call would silently break the whole
    panel for every article in the snapshot.
    """
    ids = set(ID_ATTR_RE.findall(html))
    if not ids:
        return html

    html = ID_ATTR_RE.sub(lambda m: 'id="%s--%s"' % (anchor, m.group(1)), html)

    def ref_repl(m):
        prefix, value = m.group(1), m.group(2)
        if value in ids:
            return '%s%s--%s"' % (prefix, anchor, value)
        return m.group(0)
    html = ID_REF_RE.sub(ref_repl, html)

    def js_repl(m):
        quote, value = m.group(1), m.group(2)
        if value in ids:
            return "getElementById(%s%s--%s%s)" % (quote, anchor, value, quote)
        return m.group(0)
    html = GET_ELEMENT_BY_ID_RE.sub(js_repl, html)

    return html


def inline_css():
    css = read(os.path.join(ROOT, "static", "style.css"))
    mask_uri = data_uri("static/img/logo-mask.png")
    if mask_uri:
        css = css.replace('url("img/logo-mask.png")', 'url("%s")' % mask_uri)
    return css


SNAPSHOT_CSS = """
.snapshot-banner { background: var(--accent-soft); border-bottom: 1px solid var(--accent); }
.snapshot-banner .wrap { padding: 1.4rem 24px; }
.snapshot-banner p { margin: .3rem 0; color: var(--ink-soft); }
.snapshot-toc { background: #fff; border-bottom: 1px solid #e2e2e2; }
.snapshot-toc .wrap { padding: 1.4rem 24px 1.8rem; }
.snapshot-toc h2 { margin: 0 0 .8rem; font-size: 1.1rem; }
.snapshot-toc-group { margin: 0 0 1rem; }
.snapshot-toc-group h3 { margin: 0 0 .3rem; font-size: .8rem; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); }
.snapshot-toc-group ul { margin: 0; padding: 0; list-style: none; }
.snapshot-toc-group li { margin: .15rem 0; font-size: .92rem; }
.snapshot-toc-group code { font-size: .82em; color: var(--muted); margin-left: .4em; }
.snapshot-path-bar { display: flex; flex-wrap: wrap; gap: .6rem; align-items: center; font-size: .82rem; color: var(--muted); background: #f4f6f8; border-bottom: 1px solid #e2e2e2; padding: .5rem 24px; }
.snapshot-path-bar code { color: var(--ink-soft); }
.snapshot-path-bar a { font-size: .82rem; }
.snapshot-page { border-top: 4px solid var(--accent-soft); }
"""


def build_banner(built):
    return """
<section class="snapshot-banner">
  <div class="wrap">
    <p><strong>This is a single-file, point-in-time snapshot</strong> of the
      Replication Research (R2) mirror, generated %s.</p>
    <p>Every section below is labelled with its original path on the live
      site. To cite or link to one specific page inside this archived file,
      append its path as a fragment to this file's URL or filename - e.g.
      <code>#articles/9577</code> - or use the
      <a href="#toc">table of contents</a> to jump straight to it.</p>
  </div>
</section>""" % built


def build_toc(pages):
    groups = []
    seen = []
    for anchor, label, group, _ in pages:
        if group not in seen:
            seen.append(group)
            groups.append((group, []))
        groups[seen.index(group)][1].append((anchor, label))

    parts = ['<section class="snapshot-toc" id="toc"><div class="wrap">',
             "<h2>Contents</h2>"]
    for group, items in groups:
        parts.append('<div class="snapshot-toc-group"><h3>%s</h3><ul>' % group)
        for anchor, label in items:
            parts.append('<li><a href="#%s">%s</a> <code>/%s</code></li>'
                          % (anchor, label, anchor if anchor != "home" else ""))
        parts.append("</ul></div>")
    parts.append("</div></section>")
    return "".join(parts)


def build_path_bar(anchor, label):
    path = "" if anchor == "home" else anchor
    return ('<div class="snapshot-path-bar">'
            '<span>Archived page</span> <code>/%s</code>'
            '<a href="%s%s">live version ↗</a>'
            '<a href="#toc">↑ contents</a>'
            '</div>') % (path, LIVE_BASE, path)


def main():
    if not os.path.isdir(SITE):
        raise SystemExit("_site/ not found - run: BASE_URL=/ python scripts/build.py")

    pages = collect_pages()
    home_html = read(os.path.join(SITE, "index.html"))
    header = HEADER_RE.search(home_html).group(1)
    footer = FOOTER_RE.search(home_html).group(1)
    built = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    sections = []
    for anchor, label, _group, rel_path in pages:
        full = os.path.join(SITE, rel_path)
        if not os.path.isfile(full):
            continue
        content = extract_main(read(full))
        content = strip_pdf_previews(content)
        content = namespace_page_ids(content, anchor)
        sections.append(
            '<section class="snapshot-page" id="%s">%s%s</section>'
            % (anchor, build_path_bar(anchor, label), content))

    body = "".join([
        header,
        build_banner(built),
        build_toc(pages),
        '<main id="main">',
        "".join(sections),
        "</main>",
        footer,
    ])

    body = rewrite_internal_hrefs(body)
    body = inline_images(body)

    external_link_script = EXTERNAL_SCRIPT_RE.search(home_html)
    if not external_link_script:
        raise RuntimeError("could not find the external-link script in base.html output")

    favicon_uri = data_uri("assets/img/" + os.path.basename(
        json.load(open(os.path.join(DATA, "journal.json"), encoding="utf-8"))
        ["favicon"].split("__BASE__assets/img/")[-1]))

    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Replication Research (R2) — full-text snapshot (%s)</title>
<meta name="description" content="Self-contained, citable snapshot of the Replication Research (R2) journal mirror.">
%s
<style>
%s
%s
</style>
</head>
<body>
%s
%s
</body>
</html>""" % (
        built,
        ('<link rel="icon" href="%s">' % favicon_uri) if favicon_uri else "",
        inline_css(),
        SNAPSHOT_CSS,
        body,
        external_link_script.group(1),
    )

    out_path = os.path.join(SITE, "r2-mirror-snapshot.html")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)

    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print("Wrote %s (%d sections, %.1f MB)" % (out_path, len(sections), size_mb))


if __name__ == "__main__":
    main()
