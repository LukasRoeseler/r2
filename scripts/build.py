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


def main():
    journal = load("journal.json")
    issues = load("issues.json")
    articles = load("articles.json")
    pages = load("pages.json")
    announcements = load("announcements.json")
    stats = load("stats.json")

    team = {}
    team_path = os.path.join(DATA, "team.json")
    if os.path.exists(team_path):
        team = team_with_photos(load("team.json"))

    articles_by_path = {a["urlPath"]: a for a in articles}
    for a in articles:
        a["stats"] = stats.get(a["submissionId"]) or None
        a["statsChart"] = stats_chart(a["stats"]) if a["stats"] else ""
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

    current = issues[0] if issues else None
    render("home.html", "index.html",
           current_issue=current, articles_by_path=articles_by_path,
           recent_announcements=announcements[:3])

    render("issues.html", os.path.join("issues", "index.html"))
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
        else:
            render("page.html", out, page=page)

    render("404.html", "404.html")

    # Static assets.
    shutil.copytree(os.path.join(ROOT, "assets"), os.path.join(OUT, "assets"))
    shutil.copytree(os.path.join(ROOT, "static"), os.path.join(OUT, "static"))
    open(os.path.join(OUT, ".nojekyll"), "w").close()

    n_pages = sum(len(files) for _, _, files in os.walk(OUT))
    print("Built %d files into _site/ (base URL %s)" % (n_pages, BASE))


def stats_chart(stats, width=256, height=96):
    """Inline SVG bar chart of monthly PDF downloads.

    Abstract views are deliberately not charted: OJS only counts a view when
    someone loads the abstract page on ejournals.uni-muenster.de itself, so
    a mirror visitor never registers one and the number would be misleading
    here. Downloads are still meaningful because the mirror's Download PDF
    button links straight to the canonical OJS download URL, which OJS does
    count.
    """
    downloads = stats.get("monthlyDownloads") or {}
    months = sorted(downloads)
    if len(months) < 2:
        return ""
    peak = max([*downloads.values(), 1])
    label_h = 14
    plot_h = height - label_h
    group_w = width / len(months)
    bar_w = max(3.0, min(14.0, group_w - 3.0))
    bars = []
    for i, month in enumerate(months):
        value = downloads.get(month, 0)
        h = plot_h * value / peak
        x0 = i * group_w + (group_w - bar_w) / 2
        bars.append(
            '<rect class="bar-downloads" x="%.1f" y="%.1f" width="%.1f" height="%.1f">'
            '<title>%s: %d downloads</title></rect>'
            % (x0, plot_h - h, bar_w, max(h, 0.5), month, value))
    labels = (
        '<text class="chart-label" x="0" y="%d">%s</text>'
        '<text class="chart-label" x="%d" y="%d" text-anchor="end">%s</text>'
        % (height - 2, months[0], width, height - 2, months[-1]))
    return ('<svg viewBox="0 0 %d %d" width="100%%" role="img" '
            'aria-label="Monthly PDF downloads">%s%s</svg>'
            % (width, height, "".join(bars), labels))


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

    nav = []
    about_children = []
    for path in groups["about"]["children"]:
        u = url_for(path)
        label = next((i["label"] for i in nav_items if i["path"] == path), None)
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
