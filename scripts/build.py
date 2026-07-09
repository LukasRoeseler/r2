#!/usr/bin/env python3
"""Render the static mirror site from data/*.json into _site/.

BASE_URL (env) is the path the site is served under, e.g.
"/replication-research-mirror/" for GitHub project pages or "/" for a custom
domain. The __BASE__ placeholder in harvested HTML is replaced with it.
"""

import datetime
import json
import os
import shutil
import sys
import urllib.parse

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "_site")

BASE = os.environ.get("BASE_URL", "/r2/")
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
]

SUBMISSION_RESOURCES = [
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
    issues = load("issues.json")
    articles = load("articles.json")
    pages = load("pages.json")
    announcements = load("announcements.json")
    stats = load("stats.json")

    submissions = None
    submissions_path = os.path.join(DATA, "submissions.json")
    if os.path.exists(submissions_path):
        submissions = load("submissions.json")

    team = {}
    team_path = os.path.join(DATA, "team.json")
    if os.path.exists(team_path):
        team = team_with_photos(load("team.json"))

    under_review = []
    under_review_path = os.path.join(DATA, "under_review.json")
    if os.path.exists(under_review_path):
        under_review = load("under_review.json")

    articles_by_path = {a["urlPath"]: a for a in articles}
    issues = sort_issues_newest_first(issues, articles_by_path)
    for a in articles:
        a["stats"] = normalize_article_stats(stats.get(a["submissionId"]))
        pub_month = a["datePublished"][:7] if a.get("datePublished") else None
        a["statsChart"] = stats_chart(a["stats"], pub_month) if a["stats"] else ""
        pdf = next((g for g in a["galleys"] if g["localPdf"]), None)
        a["pdf"] = pdf
        if pdf:
            a["pdfLocalUrl"] = BASE + "assets/pdf/" + pdf["localPdf"]
            a["viewerUrl"] = (BASE + "static/pdfjs/web/viewer.html?file="
                              + urllib.parse.quote(a["pdfLocalUrl"], safe="")
                              + "#zoom=page-width")
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
           under_review=under_review)

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

    for page in pages:
        out = os.path.join(*page["slug"].split("/"), "index.html")
        if page["slug"] == "about/editorialTeam" and team.get("sections"):
            render("team.html", out, page=page, team=team)
        elif page["slug"] == "about/submissions":
            render("submissions.html", out, page=page,
                   resources=SUBMISSION_RESOURCES,
                   submissions=submissions_charts)
        elif page["slug"] == "reviewer-guidelines":
            render("page.html", out, page=page,
                   resources=REVIEWER_RESOURCES,
                   resources_heading="Quick Links")
        else:
            render("page.html", out, page=page)

    render("404.html", "404.html")

    # Static assets.
    shutil.copytree(os.path.join(ROOT, "assets"), os.path.join(OUT, "assets"))
    shutil.copytree(os.path.join(ROOT, "static"), os.path.join(OUT, "static"))
    open(os.path.join(OUT, ".nojekyll"), "w").close()

    n_pages = sum(len(files) for _, _, files in os.walk(OUT))
    print("Built %d files into _site/ (base URL %s)" % (n_pages, BASE))


def stats_chart(stats, min_month=None, width=256, height=96):
    """Inline SVG grouped-bar chart of monthly OJS views and PDF downloads.

    min_month (a "YYYY-MM" string, typically the article's publication
    month) drops earlier months: an article published in April naturally
    has zero views/downloads for October through March because it didn't
    exist yet, and charting those as real zeros is misleading rather than
    informative.

    Mirror-page views (GoatCounter) are not charted here - GoatCounter's
    tracking only started once the snippet was added, so a full monthly
    history isn't meaningful yet; the total is shown as a plain number
    instead (see the article page's Usage box).
    """
    views = {m: v for m, v in (stats.get("monthlyOjsViews") or {}).items()
             if not min_month or m >= min_month}
    downloads = {m: v for m, v in (stats.get("monthlyDownloads") or {}).items()
                 if not min_month or m >= min_month}
    months = sorted(set(views) | set(downloads))
    if len(months) < 2:
        return ""
    peak = max([*views.values(), *downloads.values(), 1])
    label_h = 14
    plot_h = height - label_h
    group_w = width / len(months)
    bar_w = max(2.0, min(9.0, group_w / 2 - 1.5))
    bars = []
    for i, month in enumerate(months):
        x0 = i * group_w + (group_w - 2 * bar_w - 1) / 2
        for offset, series, css, kind in ((0, views, "bar-views", "OJS views"),
                                          (bar_w + 1, downloads, "bar-downloads",
                                           "downloads")):
            value = series.get(month, 0)
            h = plot_h * value / peak
            bars.append(
                '<rect class="%s" x="%.1f" y="%.1f" width="%.1f" height="%.1f">'
                '<title>%s: %d %s</title></rect>'
                % (css, x0 + offset, plot_h - h, bar_w, max(h, 0.5), month,
                   value, kind))
    labels = (
        '<text class="chart-label" x="0" y="%d">%s</text>'
        '<text class="chart-label" x="%d" y="%d" text-anchor="end">%s</text>'
        % (height - 2, months[0], width, height - 2, months[-1]))
    return ('<svg viewBox="0 0 %d %d" width="100%%" role="img" '
            'aria-label="Monthly OJS views and PDF downloads">%s%s</svg>'
            % (width, height, "".join(bars), labels))


def simple_bar_chart(values, css_class, aria_label, width=280, height=90):
    """Minimalist single-series monthly bar chart: bars only, no Y-axis
    numbers or gridlines - just the first and last month as x-axis labels.
    """
    months = sorted(values)
    if len(months) < 2:
        return ""
    peak = max([*values.values(), 1])
    label_h = 14
    plot_h = height - label_h
    group_w = width / len(months)
    bar_w = max(3.0, min(16.0, group_w - 3.0))
    bars = []
    for i, month in enumerate(months):
        value = values.get(month, 0)
        h = plot_h * value / peak
        x0 = i * group_w + (group_w - bar_w) / 2
        bars.append(
            '<rect class="%s" x="%.1f" y="%.1f" width="%.1f" height="%.1f">'
            '<title>%s: %d</title></rect>'
            % (css_class, x0, plot_h - h, bar_w, max(h, 0.5), month, value))
    labels = (
        '<text class="chart-label" x="0" y="%d">%s</text>'
        '<text class="chart-label" x="%d" y="%d" text-anchor="end">%s</text>'
        % (height - 2, months[0], width, height - 2, months[-1]))
    return ('<svg viewBox="0 0 %d %d" width="100%%" role="img" '
            'aria-label="%s">%s%s</svg>'
            % (width, height, aria_label, "".join(bars), labels))


def status_stacked_bar(counts, width=280, height=26):
    """A single horizontal bar split into published/under-review/declined
    segments, proportional to their share of all submissions."""
    order = [("published", "status-published", "Published"),
             ("underReview", "status-underreview", "Under review"),
             ("declined", "status-declined", "Declined")]
    total = sum(counts.get(k, 0) for k, _, _ in order) or 1
    x = 0.0
    segs = []
    for key, css, label in order:
        value = counts.get(key, 0)
        w = width * value / total
        if value:
            segs.append(
                '<rect class="%s" x="%.1f" y="0" width="%.1f" height="%d">'
                '<title>%s: %d (%.0f%%)</title></rect>'
                % (css, x, w, height, label, value, 100 * value / total))
        x += w
    return ('<svg viewBox="0 0 %d %d" width="100%%" height="%d" role="img" '
            'aria-label="Submission status breakdown">%s</svg>'
            % (width, height, height, "".join(segs)))


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


def excerpt(html, length=220):
    """Plain-text preview of an HTML fragment."""
    import re
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"\s+", " ", text).replace("&nbsp;", " ").strip()
    return text[:length].rsplit(" ", 1)[0] + "…" if len(text) > length else text


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
        if path in page_slugs:
            return BASE + path + "/"
        return None

    # Group children under "About" and "Issues" the way the OJS menu does.
    groups = {
        "about": {"label": "About", "children": [
            "about", "about/editorialTeam", "mentorshipprogram",
            "constitution",
        ]},
    }
    used = set()
    for g in groups.values():
        used.update(g["children"])

    # OJS's own nav labels, overridden for the mirror only - the URL/slug
    # stays whatever OJS uses (editorialTeam/) so links keep working.
    label_overrides = {"about/editorialTeam": "Who we are"}

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
    nav.append({"label": "Issues", "url": BASE + "issues/", "children": []})
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
