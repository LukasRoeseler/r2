#!/usr/bin/env python3
"""Render the static mirror site from data/*.json into _site/.

BASE_URL (env) is the path the site is served under. replicationresearch.
github.io is a <user-or-org>.github.io repo, so GitHub Pages serves it at
the domain root ("/") - a plain project-pages repo would need "/<repo>/"
instead. The __BASE__ placeholder in harvested HTML is replaced with it.
"""

import datetime
import html
import json
import os
import re
import shutil
import sys
import urllib.parse

from bs4 import BeautifulSoup, NavigableString
from jinja2 import Environment, FileSystemLoader, select_autoescape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from pdf_fulltext import extract_fulltext
except ImportError:  # pymupdf not installed - build without full-text sections
    def extract_fulltext(pdf_path, fig_url_prefix):
        return {"html": "", "figures": []}
    print("pymupdf not available - skipping PDF full-text extraction.")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "_site")

BASE = os.environ.get("BASE_URL", "/")
if not BASE.startswith("/"):
    BASE = "/" + BASE
if not BASE.endswith("/"):
    BASE += "/"


def load(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as fh:
        return _rebase(json.load(fh))


def _rebase(value):
    if isinstance(value, str):
        return value.replace("__BASE__", BASE)
    if isinstance(value, list):
        return [_rebase(v) for v in value]
    if isinstance(value, dict):
        return {k: _rebase(v) for k, v in value.items()}
    return value


FURTHER_PROJECTS = [
    {
        "title": "Replication Journal Federation",
        "img": "project-rjf.png",
        "url": "https://forrt.org/rjf/",
        "blurb": "A federation of journals championing replication "
                 "research together.",
    },
    {
        "title": "FLoRA Explorer",
        "img": "project-flora.png",
        "url": "https://forrt.org/flora-explorer/",
        "blurb": "Browse FORRT's Library of Reproduction and Replication "
                 "Attempts.",
    },
    {
        "title": "Handbook for Reproduction and Replication Studies",
        "img": "project-handbook.png",
        "url": "https://forrt.org/replication-handbook/",
        "blurb": "Practical, field-tested guidance for planning and "
                 "conducting replication work.",
    },
    {
        "title": "FORRT Replication Hub",
        "img": "project-hub.png",
        "url": "https://forrt.org/replication-hub/",
        "blurb": "Track, explore, disseminate, and conduct replications "
                 "in one place.",
    },
    {
        "title": "Zotero Replication Checker",
        "img": "project-zotero.png",
        "url": "https://forrt.org/flora-zotero/",
        "blurb": "A Zotero plugin that flags whether a reference in your "
                 "library has been replicated.",
    },
    {
        "title": "Love Replications Week",
        "img": "project-love-week.png",
        "url": "https://forrt.org/love-replications-week/",
        "blurb": "An annual celebration of replication research, with "
                 "events and showcases across the community.",
    },
    {
        "title": "Guess the Replication",
        "img": "project-guess.png",
        "url": "https://lukasroeseler.github.io/GuessTheReplication/",
        "blurb": "A quiz game that tests your intuitions about which "
                 "studies replicate.",
    },
    {
        "title": "Replication Atlas",
        "img": "project-atlas.svg",
        "url": "https://forrt.org/flora-replication-atlas/",
        "blurb": "Search any study to see whether it has been replicated, "
                 "powered by FORRT's FLoRA database.",
    },
]

SUBMISSION_RESOURCES = [
    {
        "title": "Manuscripts Under Review",
        "blurb": "See what submissions are currently under review.",
        "url": BASE + "#under-review",
    },
    {
        "title": "Pre-Submission Inquiry",
        "blurb": "Not sure your project is a fit? Check with the editors "
                 "before you write the full manuscript.",
        "url": "https://docs.google.com/forms/d/e/1FAIpQLScktynmACLlyYc7Ezp1dmcEWoyvr02Xy2D59F8o3JZyhCCaGw/viewform",
    },
    {
        "title": "Cover Letter Template",
        "blurb": "An optional template for the cover letter accompanying "
                 "your submission.",
        "url": "https://ejournals.uni-muenster.de/replicationresearch/libraryFiles/downloadPublic/180",
    },
    {
        "title": "Manuscript Template",
        "blurb": "An optional template for reproductions and "
                 "replications, hosted on OSF.",
        "url": "https://osf.io/brxtd/overview",
    },
]

REVIEWER_RESOURCES = [
    {
        "title": "Review Reports",
        "blurb": "Published peer review reports for completed submissions "
                 "(accepted or declined).",
        "url": "https://zenodo.org/communities/r2/records?q=review&l=list&p=1&s=10&sort=bestmatch",
    },
    {
        "title": "PubPeer Comments",
        "blurb": "Follow ongoing reviews as they happen, posted publicly "
                 "on PubPeer.",
        "url": 'https://pubpeer.com/search?q="Replication+Research+Peer+Review"',
    },
    {
        "title": "Reproducibility Certificates",
        "blurb": "Certificates confirming a submission's computational "
                 "reproducibility.",
        "url": 'https://zenodo.org/communities/r2/records?q="reproducibility%20certificate"&l=list&p=1&s=10&sort=bestmatch',
    },
]


def sort_issues_newest_first(issues, articles_by_path):
    """Newest-first order for issues and, within each issue's sections, its
    articles - rather than trusting whatever order OJS's archive/TOC pages
    happen to render in, which isn't guaranteed to stay newest-first.
    """
    def article_date(url_path):
        return (articles_by_path.get(url_path) or {}).get("datePublished") or ""

    issues = sorted(issues, key=lambda i: i.get("datePublished") or "",
                     reverse=True)
    for issue in issues:
        for section in issue["sections"]:
            section["articles"] = sorted(section["articles"],
                                          key=article_date, reverse=True)
    return issues


SURNAME_RE = re.compile(
    r"([A-ZÀ-Þ][\w'’.\-]*(?:\s[A-ZÀ-Þ][\w'’\-]*)?)"
    r"\s*,\s*[A-ZÀ-Þ]\.")

TAG_RE = re.compile(r"<[^>]*>")


def _reference_html(p):
    """Inner HTML of a reference <p>, whitespace-collapsed but with any
    <a href="https://doi.org/..."> link OJS wraps the DOI in kept intact -
    so the citation tooltip's DOI renders as a clickable link, not just
    plain text. Text nodes are escaped by hand since NavigableString yields
    already-decoded text (a literal "&" would otherwise corrupt the HTML
    fragment this gets spliced into); tag nodes serialize pre-escaped.
    """
    parts = []
    for node in p.contents:
        if isinstance(node, NavigableString):
            parts.append(html.escape(re.sub(r"\s+", " ", str(node))))
        else:
            parts.append(str(node))
    fragment = "".join(parts).strip()
    return re.sub(r"\s+([,.;:])", r"\1", fragment)


def _reference_entries(references_html):
    """[(html_fragment, surnames[], "YYYY[x]"), ...] parsed from the
    OJS-scraped reference list - one <p> per reference, APA-style "Surname,
    F., & Surname2, G. (YYYY[x]). Title...". Entries whose leading
    author/year can't be parsed are skipped (never shown as a tooltip is
    safer than a wrong one).
    """
    soup = BeautifulSoup(references_html or "", "html.parser")
    entries = []
    for p in soup.find_all("p"):
        text = re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()
        m = re.match(r"^(.*?)\((\d{4}[a-z]?)\)", text)
        if not m:
            continue
        author_segment, year = m.group(1), m.group(2)
        surnames = SURNAME_RE.findall(author_segment)
        if not surnames:
            m2 = re.match(r"^([A-ZÀ-Þ][\w'’\-]+)", author_segment.strip())
            if m2:
                surnames = [m2.group(1)]
        if surnames:
            entries.append((_reference_html(p), surnames, year))
    return entries


def linkify_citation(citation, doi_url):
    """HTML version of the plain-text CSL citation with its DOI URL (which
    OJS's csl-entry always spells out verbatim at the end) turned into a
    clickable link, so "How to cite" doesn't show it as inert text.
    """
    if not citation:
        return ""
    escaped = html.escape(citation)
    if doi_url:
        needle = html.escape(doi_url)
        if needle in escaped:
            escaped = escaped.replace(
                needle, '<a href="%s">%s</a>' % (needle, needle), 1)
    return escaped


def link_citations(fulltext_html, references_html):
    """Wrap in-text citations like "Dreber &amp; Johannesson (2025a)" with a
    hoverable tooltip showing the matching full reference - from OJS's
    reference list (the authoritative source both in the sidebar and here;
    full-text extraction already skips the PDF's own copy of it), not the
    PDF's own in-text formatting.
    """
    entries = _reference_entries(references_html)
    if not entries:
        return fulltext_html

    lookup = {}
    for full_html, surnames, year in entries:
        if len(surnames) == 1:
            who = surnames[0]
        elif len(surnames) == 2:
            who = "%s & %s" % (surnames[0], surnames[1])
        else:
            who = "%s et al." % surnames[0]
        who_amp = who.replace("&", "&amp;")
        full = full_html
        # Every surface form the citation might take in-text: narrative
        # "(YYYY)", and the two parenthetical spacings seen in practice -
        # some journals put a comma before the year, some don't (the latter
        # also covers multi-citation groups like "(A 2020; B et al. 2021)",
        # since each piece is matched on its own without the shared parens).
        for surface in ("%s (%s)" % (who_amp, year),
                        "%s, %s" % (who_amp, year),
                        "%s %s" % (who_amp, year)):
            lookup.setdefault(surface, full)

    if not lookup:
        return fulltext_html
    # Longest surface text first, so a 3-author "et al." form can't be
    # pre-empted by a shorter, coincidentally-overlapping match.
    surfaces = sorted(lookup, key=len, reverse=True)
    pattern = re.compile("(" + "|".join(re.escape(s) for s in surfaces) + ")")

    def repl(m):
        surface = m.group(1)
        return ('<span class="cite-ref" tabindex="0">%s'
                '<span class="cite-tooltip">%s</span></span>'
                % (surface, lookup[surface]))

    # Substitute only in the text between tags, never inside a tag itself -
    # a figure caption (rendered into alt="...") that happens to match a
    # citation surface would otherwise get tooltip markup injected into the
    # attribute value, breaking it.
    tags = TAG_RE.findall(fulltext_html)
    parts = TAG_RE.split(fulltext_html)
    out = [pattern.sub(repl, parts[0])]
    for tag, part in zip(tags, parts[1:]):
        out.append(tag)
        out.append(pattern.sub(repl, part))
    return "".join(out)


def build_articles_index(articles):
    """Payload for the client-side Articles browsing page: one compact
    record per article - search/filter/sort all happen in the browser
    against this, so it only carries what's shown or filtered on, not
    full abstracts/references - plus the distinct sections/categories and
    year span needed to populate the filter controls.
    """
    records = []
    years = []
    sections, categories = set(), set()
    for a in articles:
        year = None
        if a.get("datePublished"):
            try:
                year = int(a["datePublished"][:4])
            except ValueError:
                pass
        if year:
            years.append(year)
        if a.get("section"):
            sections.add(a["section"])
        categories.update(a.get("categories") or [])
        stats = a.get("stats") or {}
        records.append({
            "title": a["title"],
            "subtitle": a.get("subtitle") or "",
            "url": "articles/%s/" % a["urlPath"],
            "section": a.get("section") or "",
            "categories": a.get("categories") or [],
            "authors": [au["name"] for au in a.get("authors") or []],
            "year": year,
            "date": a.get("datePublished") or "",
            "views": stats.get("totalViews"),
            "downloads": stats.get("pdfDownloads"),
        })
    records.sort(key=lambda r: r["date"], reverse=True)
    this_year = datetime.date.today().year
    return {
        "records": records,
        "sections": sorted(sections),
        "categories": sorted(categories),
        "yearMin": min(years) if years else this_year,
        "yearMax": max(years) if years else this_year,
    }


def normalize_article_stats(stats):
    """Fill in missing keys with None so templates never render a blank
    number, and compute the combined view count shown in the compact
    article-card pill (OJS abstract views + GoatCounter mirror views).
    """
    if not stats:
        return None
    ojs_views = stats.get("ojsViews")
    mirror_views = stats.get("mirrorViews")
    parts, total = [], 0
    if ojs_views is not None:
        total += ojs_views
        parts.append("%d on OJS" % ojs_views)
    if mirror_views is not None:
        total += mirror_views
        parts.append("%d on this mirror" % mirror_views)
    stats["ojsViews"] = ojs_views
    stats["mirrorViews"] = mirror_views
    stats["pdfDownloads"] = stats.get("pdfDownloads")
    stats["totalViews"] = total if parts else None
    stats["viewsBreakdown"] = " · ".join(parts) if parts else ""
    return stats


def main():
    journal = load("journal.json")
    # The scraped footer HTML ends with a "hosted by hbz" logo paragraph.
    # Pull it out so the template can group it next to the OJS/PKP badge
    # (both are "who runs this" credits) instead of leaving it stranded as
    # a large logo on its own row. Falls back gracefully if the markup or
    # the image ever changes: the logo just stays in footerHtml.
    footer_html = journal.get("footerHtml") or ""
    m = re.search(r"<p>\s*(<a\b[^>]*>)?\s*<img\b[^>]*>\s*(</a>)?\s*</p>",
                  footer_html, re.I | re.S)
    if m:
        journal["hostedByHtml"] = m.group(0)
        journal["footerHtml"] = footer_html[:m.start()] + footer_html[m.end():]
    else:
        journal["hostedByHtml"] = ""
    issues = load("issues.json")
    articles = load("articles.json")
    pages = load("pages.json")
    announcements = load("announcements.json")
    stats = load("stats.json")

    submissions = None
    submissions_path = os.path.join(DATA, "submissions.json")
    if os.path.exists(submissions_path):
        submissions = load("submissions.json")

    # Shown on both the Submissions page and the homepage's Under Review
    # card - computed once here so both render() calls can share it.
    submissions_charts = None
    if submissions and submissions.get("monthly"):
        submissions_charts = {
            "monthlyChart": simple_bar_chart(
                submissions["monthly"], "bar-submissions",
                "New submissions per month"),
            "statusBar": status_stacked_bar(submissions["statusCounts"]),
            "statusCounts": submissions["statusCounts"],
            "total": submissions["total"],
        }

    team = {}
    team_path = os.path.join(DATA, "team.json")
    if os.path.exists(team_path):
        team = team_with_photos(load("team.json"))

    under_review = []
    under_review_path = os.path.join(DATA, "under_review.json")
    if os.path.exists(under_review_path):
        under_review = load("under_review.json")

    published_extras = {}
    published_extras_path = os.path.join(DATA, "published_extras.json")
    if os.path.exists(published_extras_path):
        published_extras = load("published_extras.json")

    articles_by_path = {a["urlPath"]: a for a in articles}
    issues = sort_issues_newest_first(issues, articles_by_path)
    # Figure PNGs extracted from PDFs, held in memory until after the
    # assets/static copytree calls (their destination must not exist yet).
    fulltext_figures = []
    for a in articles:
        a["stats"] = normalize_article_stats(stats.get(a["submissionId"]))
        pub_month = a["datePublished"][:7] if a.get("datePublished") else None
        a["statsChart"] = stats_chart(a["stats"], pub_month) if a["stats"] else ""
        extras = published_extras.get((a.get("doi") or "").strip().lower()) or {}
        a["materials"] = [
            {"label": label, "url": extras[key]}
            for key, label in (("peerReviewUrl", "Peer Review Report"),
                               ("reproCertUrl", "Repro. Certificate"),
                               ("dataUrl", "Data"),
                               ("materialsUrl", "Materials"))
            if extras.get(key)
        ]
        a["laySummary"] = extras.get("laySummary") or ""
        a["citationHtml"] = linkify_citation(a.get("citation"), a.get("doiUrl"))
        pdf = next((g for g in a["galleys"] if g["localPdf"]), None)
        a["pdf"] = pdf
        a["fullTextHtml"] = ""
        if pdf:
            a["pdfLocalUrl"] = BASE + "assets/pdf/" + pdf["localPdf"]
            a["viewerUrl"] = (BASE + "static/pdfjs/web/viewer.html?file="
                              + urllib.parse.quote(a["pdfLocalUrl"], safe="")
                              + "#zoom=page-width")
            pdf_path = os.path.join(ROOT, "assets", "pdf", pdf["localPdf"])
            if os.path.exists(pdf_path):
                prefix = BASE + "assets/fulltext/" + a["urlPath"] + "-"
                result = extract_fulltext(pdf_path, prefix)
                full_html = result["html"]
                if full_html and a.get("referencesHtml"):
                    full_html = link_citations(full_html, a["referencesHtml"])
                a["fullTextHtml"] = full_html
                for name, png in result["figures"]:
                    fulltext_figures.append(
                        (a["urlPath"] + "-" + name, png))
        else:
            a["pdfLocalUrl"] = a["viewerUrl"] = ""

    # Navigation: group nav entries that share a top-level dropdown in OJS.
    nav = build_nav(journal["nav"], pages)

    env = Environment(
        loader=FileSystemLoader(os.path.join(ROOT, "templates")),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["excerpt"] = excerpt

    common = {
        "base": BASE,
        "journal": journal,
        "nav": nav,
        "issues": issues,
        "announcements": announcements,
        "built": datetime.datetime.now(datetime.timezone.utc)
                 .strftime("%Y-%m-%d %H:%M UTC"),
    }

    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)

    def render(template, out_path, **ctx):
        html = env.get_template(template).render(**common, **ctx)
        target = os.path.join(OUT, out_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(html)

    render("home.html", "index.html",
           articles_by_path=articles_by_path,
           recent_announcements=announcements[:3],
           further_projects=FURTHER_PROJECTS,
           under_review=under_review,
           submissions=submissions_charts)

    articles_index = build_articles_index(articles)
    render("articles.html", os.path.join("articles", "index.html"),
           articles_index=articles_index,
           articles_json=json.dumps(articles_index, ensure_ascii=False)
                             .replace("</", "<\\/"))

    network_blocks = [b for b in journal.get("sidebarBlocks") or []
                       if not b.get("titleHidden")]
    render("network.html", os.path.join("network", "index.html"),
           network_blocks=network_blocks)

    render("issues.html", os.path.join("issues", "index.html"),
           articles_by_path=articles_by_path)
    for issue in issues:
        render("issue.html", os.path.join("issues", issue["id"], "index.html"),
               issue=issue, articles_by_path=articles_by_path)

    for a in articles:
        render("article.html",
               os.path.join("articles", a["urlPath"], "index.html"), article=a)

    render("announcements.html",
           os.path.join("announcements", "index.html"))
    for ann in announcements:
        render("announcement.html",
               os.path.join("announcements", ann["id"], "index.html"),
               announcement=ann)

    for page in pages:
        out = os.path.join(*page["slug"].split("/"), "index.html")
        if page["slug"] == "about/editorialTeam" and team.get("sections"):
            render("team.html", out, page=page, team=team)
        elif page["slug"] == "about/submissions":
            guide_html, guide_toc = sectionize_guide(page["html"])
            render("submissions.html", out, page=page,
                   resources=SUBMISSION_RESOURCES,
                   submissions=submissions_charts,
                   guide_html=guide_html, guide_toc=guide_toc)
        elif page["slug"] == "reviewer-guidelines":
            render("page.html", out, page=page,
                   resources=REVIEWER_RESOURCES,
                   resources_heading="Quick Links")
        else:
            render("page.html", out, page=page)

    render("404.html", "404.html")

    # IONOS forwarding can briefly expose its internal /defaultsite fallback
    # path. Keep that implementation detail from stranding visitors on a 404.
    defaultsite_redirect = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="0; url=/">
  <link rel="canonical" href="/">
  <script>window.location.replace("/");</script>
  <title>Redirecting…</title>
</head>
<body><p><a href="/">Continue to Replication Research</a></p></body>
</html>
"""
    for redirect_path in ("defaultsite.html",
                          os.path.join("defaultsite", "index.html")):
        target = os.path.join(OUT, redirect_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(defaultsite_redirect)

    # Static assets.
    shutil.copytree(os.path.join(ROOT, "assets"), os.path.join(OUT, "assets"))
    shutil.copytree(os.path.join(ROOT, "static"), os.path.join(OUT, "static"))
    if fulltext_figures:
        fig_dir = os.path.join(OUT, "assets", "fulltext")
        os.makedirs(fig_dir, exist_ok=True)
        for name, png in fulltext_figures:
            with open(os.path.join(fig_dir, name), "wb") as fh:
                fh.write(png)
    open(os.path.join(OUT, ".nojekyll"), "w").close()

    n_pages = sum(len(files) for _, _, files in os.walk(OUT))
    print("Built %d files into _site/ (base URL %s)" % (n_pages, BASE))


def stats_chart(stats, min_month=None, width=256, height=96):
    """Inline SVG grouped-bar chart of monthly views and PDF downloads.

    The views series is OJS abstract views plus GoatCounter mirror views,
    summed per month - the same combination shown as the single "views"
    number on article cards, just over time. Downloads come straight from
    OJS (the mirror's Download button hits the canonical OJS URL, so OJS
    counts those too).

    min_month (a "YYYY-MM" string, typically the article's publication
    month) drops earlier months: an article published in April naturally
    has zero views/downloads for October through March because it didn't
    exist yet, and charting those as real zeros is misleading rather than
    informative.
    """
    ojs = stats.get("monthlyOjsViews") or {}
    mirror = stats.get("monthlyMirrorViews") or {}
    views = {m: ojs.get(m, 0) + mirror.get(m, 0)
             for m in set(ojs) | set(mirror)
             if not min_month or m >= min_month}
    downloads = {m: v for m, v in (stats.get("monthlyDownloads") or {}).items()
                 if not min_month or m >= min_month}
    months = sorted(set(views) | set(downloads))
    if len(months) < 2:
        return ""
    peak = max([*views.values(), *downloads.values(), 1])
    label_h = 14
    axis_w = 30       # room left of the y-axis for its tick labels
    plot_h = height - label_h
    x0, x1 = axis_w + 4, width - 6
    step = (x1 - x0) / (len(months) - 1)

    def point(i, value):
        # 4px headroom so the peak's dot isn't clipped at the top edge.
        return (x0 + i * step,
                4 + (plot_h - 8) * (1 - value / peak))

    parts = [
        '<line class="chart-axis" x1="%d" y1="2" x2="%d" y2="%d"/>'
        % (axis_w, axis_w, plot_h),
        '<line class="chart-axis" x1="%d" y1="%d" x2="%d" y2="%d"/>'
        % (axis_w, plot_h, width, plot_h),
        '<text class="chart-label" x="%d" y="10" text-anchor="end">%d</text>'
        % (axis_w - 4, peak),
        '<text class="chart-label" x="%d" y="%d" text-anchor="end">0</text>'
        % (axis_w - 4, plot_h),
    ]
    for series, css, kind in ((views, "views", "views"),
                              (downloads, "downloads", "downloads")):
        coords = [point(i, series.get(m, 0)) for i, m in enumerate(months)]
        parts.append('<polyline class="line-%s" points="%s"/>'
                     % (css, " ".join("%.1f,%.1f" % c for c in coords)))
        for (x, y), month in zip(coords, months):
            parts.append(
                '<circle class="dot-%s" cx="%.1f" cy="%.1f" r="2.5">'
                '<title>%s: %d %s</title></circle>'
                % (css, x, y, month, series.get(month, 0), kind))
    parts.append(
        '<text class="chart-label" x="%d" y="%d">%s</text>'
        '<text class="chart-label" x="%d" y="%d" text-anchor="end">%s</text>'
        % (axis_w, height - 2, months[0], width, height - 2, months[-1]))
    return ('<svg viewBox="0 0 %d %d" width="100%%" role="img" '
            'aria-label="Monthly readers: views and PDF downloads per month">'
            '%s</svg>' % (width, height, "".join(parts)))


def simple_bar_chart(values, css_class, aria_label, width=280, height=90):
    """Minimalist single-series monthly bar chart: a y-axis with the peak
    value and 0, and the first/last month as x-axis labels - the same
    minimal axis treatment as stats_chart(), just for a single series.
    """
    months = sorted(values)
    if len(months) < 2:
        return ""
    peak = max([*values.values(), 1])
    label_h = 14
    axis_w = 30       # room left of the y-axis for its tick labels
    plot_h = height - label_h
    group_w = (width - axis_w) / len(months)
    bar_w = max(3.0, min(16.0, group_w - 3.0))
    bars = [
        '<line class="chart-axis" x1="%d" y1="2" x2="%d" y2="%d"/>'
        % (axis_w, axis_w, plot_h),
        '<line class="chart-axis" x1="%d" y1="%d" x2="%d" y2="%d"/>'
        % (axis_w, plot_h, width, plot_h),
        '<text class="chart-label" x="%d" y="10" text-anchor="end">%d</text>'
        % (axis_w - 4, peak),
        '<text class="chart-label" x="%d" y="%d" text-anchor="end">0</text>'
        % (axis_w - 4, plot_h),
    ]
    for i, month in enumerate(months):
        value = values.get(month, 0)
        h = plot_h * value / peak
        x0 = axis_w + i * group_w + (group_w - bar_w) / 2
        bars.append(
            '<rect class="%s" x="%.1f" y="%.1f" width="%.1f" height="%.1f">'
            '<title>%s: %d</title></rect>'
            % (css_class, x0, plot_h - h, bar_w, max(h, 0.5), month, value))
    labels = (
        '<text class="chart-label" x="%d" y="%d">%s</text>'
        '<text class="chart-label" x="%d" y="%d" text-anchor="end">%s</text>'
        % (axis_w, height - 2, months[0], width, height - 2, months[-1]))
    return ('<svg viewBox="0 0 %d %d" width="100%%" role="img" '
            'aria-label="%s">%s%s</svg>'
            % (width, height, aria_label, "".join(bars), labels))


def status_stacked_bar(counts):
    """A single horizontal bar split into published/under-review/declined
    segments, proportional to their share of all submissions. Plain HTML
    (not SVG) with a hover/focus-revealed tooltip per segment, mirroring
    the in-text citation tooltip - a native SVG <title> looked right in
    testing (the text content is correct) but is unreliable in practice:
    support for hovering an SVG shape to trigger its title varies by
    browser, with no touch equivalent at all, so it silently never showed
    for some visitors.
    """
    order = [("published", "status-published", "Published"),
             ("underReview", "status-underreview", "Under review"),
             ("declined", "status-declined", "Declined")]
    total = sum(counts.get(k, 0) for k, _, _ in order) or 1
    segs = []
    for key, css, label in order:
        value = counts.get(key, 0)
        if not value:
            continue
        pct = 100 * value / total
        segs.append(
            '<span class="status-segment %s" style="width:%.3f%%" tabindex="0">'
            '<span class="status-tooltip">%s: %d (%.0f%%)</span></span>'
            % (css, pct, label, value, pct))
    return ('<span class="status-bar" role="img" '
            'aria-label="Submission status breakdown">%s</span>'
            % "".join(segs))


TEAM_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def team_with_photos(team):
    """Attach photo URLs (assets/team/<slug>.*) and initials to members."""
    team_dir = os.path.join(ROOT, "assets", "team")
    for section in team.get("sections", []):
        for member in section["members"]:
            slug = member["slug"]
            member["photo"] = ""
            for ext in TEAM_IMG_EXTS:
                if os.path.exists(os.path.join(team_dir, slug + ext)):
                    member["photo"] = BASE + "assets/team/" + slug + ext
                    break
            parts = [p for p in member["name"].split() if p]
            member["initials"] = (parts[0][0] + parts[-1][0]).upper() \
                if len(parts) > 1 else (parts[0][:2].upper() if parts else "?")
            # Deterministic accent tone for the placeholder circle.
            member["hue"] = sum(ord(c) for c in slug) % 5
    return team


def excerpt(fragment, length=220):
    """Plain-text preview of an HTML fragment."""
    text = re.sub(r"<[^>]+>", " ", fragment or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:length].rsplit(" ", 1)[0] + "…" if len(text) > length else text


def sectionize_guide(page_html):
    """Wrap each top-level <h2> and the content up to the next one in a
    collapsible <details>, and build a matching table of contents - used
    for the long Submissions/guidelines page only, so its many sections can
    be hidden/expanded individually instead of one long wall of text.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    headings = soup.find_all("h2")
    if not headings:
        return page_html, []

    toc = []
    seen_ids = set()
    for i, h2 in enumerate(headings):
        text = h2.get_text(" ", strip=True)
        slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section-%d" % i
        unique, n = slug, 2
        while unique in seen_ids:
            unique = "%s-%d" % (slug, n)
            n += 1
        seen_ids.add(unique)
        h2["id"] = unique
        toc.append((unique, text))

        details = soup.new_tag("details", **{"class": "guide-section", "open": ""})
        summary = soup.new_tag("summary")
        body = soup.new_tag("div", **{"class": "guide-section-body"})
        h2.insert_before(details)
        summary.append(h2.extract())
        details.append(summary)
        details.append(body)

        next_h2 = headings[i + 1] if i + 1 < len(headings) else None
        node = details.next_sibling
        while node is not None and node is not next_h2:
            following = node.next_sibling
            body.append(node.extract())
            node = following

    return str(soup), toc


def build_nav(nav_items, pages):
    """Recreate the OJS two-level nav as [{label, url, children:[...]}].

    OJS flattens dropdowns in our scrape (parent followed by its children),
    so rebuild groups from the known structure: a nav item whose path is a
    mirrored page or an index route becomes a link; the OJS nav order kept.
    """
    page_slugs = {p["slug"] for p in pages}

    def url_for(path):
        if path in ("home", "index"):
            return BASE
        if path in ("issues", "issue/archive", "issue/current"):
            return BASE + "issues/"
        if path == "network":  # synthetic page, not scraped from OJS
            return BASE + "network/"
        if path in page_slugs:
            return BASE + path + "/"
        return None

    # Group children under "About" and "Issues" the way the OJS menu does.
    groups = {
        "about": {"label": "About", "children": [
            "about", "about/editorialTeam", "mentorshipprogram",
            "constitution", "network",
        ]},
    }
    used = set()
    for g in groups.values():
        used.update(g["children"])

    # OJS's own nav labels, overridden for the mirror only - the URL/slug
    # stays whatever OJS uses (editorialTeam/) so links keep working.
    label_overrides = {"about/editorialTeam": "Who we are", "network": "Our Network"}

    nav = []
    about_children = []
    for path in groups["about"]["children"]:
        u = url_for(path)
        label = label_overrides.get(path) or next(
            (i["label"] for i in nav_items if i["path"] == path), None)
        if u and label:
            about_children.append({"label": label, "url": u})
    if about_children:
        nav.append({"label": "About", "url": about_children[0]["url"],
                    "children": about_children})
    nav.append({"label": "Articles", "url": BASE + "articles/", "children": []})
    nav.append({"label": "Announcements", "url": BASE + "announcements/",
                "children": []})
    seen = {"issues", "issue/current", "issue/archive", "home", "index",
            "announcement"} | used
    for item in nav_items:
        if item["path"] in seen:
            continue
        seen.add(item["path"])
        u = url_for(item["path"])
        if u:
            nav.append({"label": item["label"], "url": u, "children": []})
    return nav


if __name__ == "__main__":
    main()
