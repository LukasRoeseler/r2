#!/usr/bin/env python3
"""Fetch all public content of Replication Research (R2) from OJS.

Sources:
  * HTML scraping (anonymous)   -> issues, articles, static pages, announcements
  * OJS REST API (OJS_API_KEY)  -> OJS abstract views, PDF downloads, and the
                                   submission funnel (published/under review/
                                   declined) for the Submissions page charts
  * GoatCounter API (optional,
    GOATCOUNTER_API_TOKEN)      -> mirror-page view counts (see base.html for
                                   the tracking snippet)
  * Editors' Google Sheet (view-only CSV export) + Crossref
                                 -> the home page's "Under Review" list
  * PDF galleys                 -> mirrored into assets/pdf/ so the GitHub
                                   Pages site can render them same-origin with
                                   pdf.js (the OJS server sends no CORS headers)

Writes data/*.json. All stored HTML fragments use the placeholder __BASE__ for
the site root; build.py replaces it with the configured base URL.

Exits non-zero if the harvest looks broken (empty issue list, missing content
containers, ...) so a failed run never wipes the published site.
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.parse

import requests
from bs4 import BeautifulSoup

SITE = "https://ejournals.uni-muenster.de"
JOURNAL = SITE + "/replicationresearch"
JOURNAL_ALT = SITE + "/index.php/replicationresearch"
API_BASE = JOURNAL_ALT + "/api/v1"
DATE_START = "2025-10-01"  # journal launch; nothing to query earlier

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
PDF_DIR = os.path.join(ROOT, "assets", "pdf")
IMG_DIR = os.path.join(ROOT, "assets", "img")

REQUEST_DELAY = 0.7  # politeness delay between requests to the OJS server

session = requests.Session()
session.headers.update({
    "User-Agent": "R2-mirror-bot/1.0 (GitHub Pages mirror; "
                  "contact@replicationresearch.org)",
})

_last_request = [0.0]


def _throttle():
    wait = REQUEST_DELAY - (time.time() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    _last_request[0] = time.time()


def get(url, **kw):
    """Polite GET with retries. Returns a Response or raises."""
    last = None
    for attempt in range(3):
        _throttle()
        try:
            r = session.get(url, timeout=60, **kw)
            if r.status_code == 200:
                return r
            last = "HTTP %s" % r.status_code
        except requests.RequestException as e:
            last = str(e)
        time.sleep(2 * (attempt + 1))
    raise RuntimeError("GET failed for %s: %s" % (url, last))


def soup_of(url):
    return BeautifulSoup(get(url).text, "html.parser")


# ---------------------------------------------------------------------------
# OJS API auth (pattern proven in the r2d2 repo: Bearer header, with a
# ?apiToken= fallback for the Apache header-stripping case, pkp-lib #9320)
# ---------------------------------------------------------------------------

def _load_env_file():
    """Populate os.environ from a git-ignored .env, for local runs."""
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env_file()
API_KEY = os.environ.get("OJS_API_KEY", "").strip()
USE_QUERY_TOKEN = False


def api_get(path, params=None, retries=3, _allow_fallback=True):
    """GET an OJS API endpoint as JSON, or None on failure."""
    global USE_QUERY_TOKEN
    if not API_KEY:
        return None
    last = None
    for attempt in range(retries):
        q = dict(params or {})
        headers = {}
        if USE_QUERY_TOKEN:
            q["apiToken"] = API_KEY
        else:
            headers["Authorization"] = "Bearer " + API_KEY
        url = API_BASE + path + ("?" + urllib.parse.urlencode(q) if q else "")
        _throttle()
        try:
            r = session.get(url, headers=headers, timeout=60)
            if r.status_code == 200:
                return r.json()
            last = "HTTP %s | %s" % (r.status_code, r.text[:200])
            if r.status_code in (401, 403):
                print("API auth error:", last, file=sys.stderr)
                if r.status_code == 403 and not USE_QUERY_TOKEN and _allow_fallback:
                    USE_QUERY_TOKEN = True
                    result = api_get(path, params, retries=1, _allow_fallback=False)
                    if result is not None:
                        print("  -> ?apiToken= query auth works; using it "
                              "for the rest of the run.", file=sys.stderr)
                        return result
                    USE_QUERY_TOKEN = False
                return None
        except requests.RequestException as e:
            last = str(e)
        time.sleep(2 * (attempt + 1))
    print("API giving up on %s: %s" % (path, last), file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# URL rewriting and asset localisation
# ---------------------------------------------------------------------------

PAGE_SLUGS = set()  # filled once nav pages are discovered


def journal_path(url):
    """Return the path of a URL inside this journal, else None.

    e.g. https://.../replicationresearch/article/view/ie -> 'article/view/ie'
    Handles both the short and the index.php URL forms.
    """
    for base in (JOURNAL_ALT, JOURNAL):
        if url.startswith(base + "/"):
            rest = url[len(base) + 1:]
            return rest.split("#")[0].split("?")[0].strip("/")
    return None


def rewrite_url(url):
    """Map an OJS URL to a mirror-local __BASE__ URL where possible."""
    path = journal_path(url)
    if path is None:
        return url
    m = re.match(r"article/view/([^/]+)(?:/.*)?$", path)
    if m:
        return "__BASE__articles/%s/" % m.group(1)
    m = re.match(r"issue/view/([^/]+)$", path)
    if m:
        return "__BASE__issues/%s/" % m.group(1)
    if path in ("issue/archive", "issues", "issue/current"):
        return "__BASE__issues/"
    m = re.match(r"announcement/view/([^/]+)$", path)
    if m:
        return "__BASE__announcements/%s/" % m.group(1)
    if path == "announcement":
        return "__BASE__announcements/"
    if path in ("index", "home", ""):
        return "__BASE__"
    if path in PAGE_SLUGS:
        return "__BASE__%s/" % path
    return url  # anything else (login, search, wizard, downloads) stays on OJS


_img_cache = {}


def localize_image(src, page_url):
    """Download an image into assets/img/ and return its __BASE__ URL.

    Falls back to the original absolute URL on any failure.
    """
    if not src or src.startswith("data:"):
        return src
    absolute = urllib.parse.urljoin(page_url, src)
    if absolute in _img_cache:
        return _img_cache[absolute]
    name = os.path.basename(urllib.parse.urlparse(absolute).path)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "img"
    digest = hashlib.sha1(absolute.encode()).hexdigest()[:8]
    filename = "%s-%s" % (digest, name)
    target = os.path.join(IMG_DIR, filename)
    local = "__BASE__assets/img/" + filename
    if os.path.exists(target):
        _img_cache[absolute] = local
        return local
    try:
        r = get(absolute)
        if len(r.content) > 15 * 1024 * 1024:
            raise RuntimeError("image too large")
        with open(target, "wb") as fh:
            fh.write(r.content)
        _img_cache[absolute] = local
        return local
    except Exception as e:  # noqa: BLE001 - keep remote URL on any failure
        print("  image kept remote (%s): %s" % (e, absolute), file=sys.stderr)
        _img_cache[absolute] = absolute
        return absolute


def clean_fragment(node, page_url):
    """Rewrite links + images of a BeautifulSoup node, return inner HTML."""
    # Scraped content may itself contain landmark tags (e.g. Quarto's <main>);
    # demote them so the mirror keeps exactly one <main> per page.
    for landmark in node.find_all(["main", "header", "footer"]):
        landmark.name = "div"
    for a in node.find_all("a", href=True):
        absolute = urllib.parse.urljoin(page_url, a["href"])
        a["href"] = rewrite_url(absolute)
    for img in node.find_all("img"):
        src = img.get("src")
        if src:
            img["src"] = localize_image(src, page_url)
        img.attrs.pop("srcset", None)
    return node.decode_contents().strip()


# ---------------------------------------------------------------------------
# Scrapers
# ---------------------------------------------------------------------------

def text_of(node):
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)) if node else ""


def scrape_journal():
    print("Scraping journal homepage ...")
    url = JOURNAL + "/index"
    doc = soup_of(url)

    def meta(name, prop=False):
        tag = doc.find("meta", attrs={("property" if prop else "name"): name})
        return tag["content"].strip() if tag and tag.get("content") else ""

    journal = {
        "name": meta("og:title", prop=True) or "Replication Research",
        "description": meta("description"),
        "keywords": meta("keywords"),
        "issn": "",
        "ojsUrl": JOURNAL,
        "submitUrl": JOURNAL + "/submission/wizard",
        "submissionsInfoUrl": JOURNAL + "/about/submissions",
        "logo": "",
        "favicon": "",
        "homepageHtml": "",
        "sidebarBlocks": [],
        "footerHtml": "",
        "nav": [],
    }

    logo = doc.select_one(".pkp_site_name img")
    if logo and logo.get("src"):
        journal["logo"] = localize_image(logo["src"], url)
    icon = doc.find("link", rel="icon")
    if icon and icon.get("href"):
        journal["favicon"] = localize_image(icon["href"], url)

    footer = doc.select_one(".pkp_footer_content")
    if footer:
        issn = re.search(r"ISSN:\s*([\d-]+X?)", footer.get_text())
        if issn:
            journal["issn"] = issn.group(1)

    # Primary navigation: discover journal pages to mirror.
    nav = []
    for a in doc.select("#navigationPrimary a[href]"):
        path = journal_path(a["href"])
        label = text_of(a)
        if path is None or not label:
            continue
        nav.append({"label": label, "path": path})
    journal["nav"] = nav
    # Page slugs must be known before any fragment is cleaned, so that links
    # to mirrored pages get rewritten to __BASE__ URLs.
    PAGE_SLUGS.update(discover_page_paths(nav))

    # Custom sidebar blocks (Organizers, Funders, Partners, PCIs, Links, ...)
    for block in doc.select(".pkp_structure_sidebar .pkp_block.block_custom"):
        title = block.find("h2")
        content = block.select_one(".content")
        if not content:
            continue
        journal["sidebarBlocks"].append({
            "id": block.get("id", ""),
            "title": text_of(title),
            "titleHidden": bool(title and "pkp_screen_reader"
                                in (title.get("class") or [])),
            "html": clean_fragment(content, url),
        })

    extra = doc.select_one(".additional_content")
    if extra:
        journal["homepageHtml"] = clean_fragment(extra, url)
    if footer:
        journal["footerHtml"] = clean_fragment(footer, url)
    return journal


def discover_page_paths(nav):
    """Journal pages worth mirroring, from nav + known extras."""
    skip = {
        "issues", "issue/current", "issue/archive", "search", "login",
        "user/register", "index", "home", "announcement",
        "about/contact",  # not mirrored; keep the OJS site as the contact point
    }
    paths = []
    for item in nav:
        p = item["path"]
        if p in skip or p.startswith(("issue/", "article/", "user/")):
            continue
        if p not in paths:
            paths.append(p)
    for extra in ("legal-disclosure",):
        if extra not in paths:
            paths.append(extra)
    return paths


def scrape_page(path):
    url = JOURNAL + "/" + path
    print("Scraping page /%s ..." % path)
    doc = soup_of(url)
    main = doc.select_one(".pkp_structure_main .page")
    if main is None:
        raise RuntimeError("no .page container on %s" % url)
    crumbs = main.select_one("nav.cmp_breadcrumbs")
    if crumbs:
        crumbs.decompose()
    h1 = main.find("h1")
    title = text_of(h1)
    if h1:
        h1.decompose()
    return {
        "slug": path,
        "title": title,
        "html": clean_fragment(main, url),
        "sourceUrl": url,
    }


def scrape_issues():
    print("Scraping issue archive ...")
    archive_url = JOURNAL + "/issue/archive"
    doc = soup_of(archive_url)
    issues = []
    for summary in doc.select(".obj_issue_summary"):
        a = summary.select_one("a.title")
        if not a:
            continue
        m = re.match(r"issue/view/([^/]+)$", journal_path(a["href"]) or "")
        if not m:
            continue
        issue_id = m.group(1)
        cover = summary.select_one(".cover img")
        series = summary.select_one(".series")
        desc = summary.select_one(".description")
        issues.append({
            "id": issue_id,
            "title": text_of(a),
            "series": text_of(series),
            "descriptionHtml": clean_fragment(desc, archive_url) if desc else "",
            "cover": localize_image(cover["src"], archive_url)
                     if cover and cover.get("src") else "",
            "ojsUrl": JOURNAL + "/issue/view/" + issue_id,
            "datePublished": "",
            "sections": [],
        })

    for issue in issues:
        print("Scraping issue %s ..." % issue["id"])
        try:
            doc = soup_of(issue["ojsUrl"])
            toc = doc.select_one(".obj_issue_toc")
            if toc is None:
                raise RuntimeError("no TOC on issue %s" % issue["id"])
            published = toc.select_one(".heading .published .value")
            issue["datePublished"] = text_of(published)
            desc = toc.select_one(".heading .description")
            if desc and not issue["descriptionHtml"]:
                issue["descriptionHtml"] = clean_fragment(desc, issue["ojsUrl"])
            for section in toc.select(".sections .section"):
                heading = section.find("h2")
                entry = {"title": text_of(heading), "articles": []}
                for item in section.select(".obj_article_summary"):
                    a = item.select_one(".title a")
                    if not a:
                        continue
                    m = re.match(r"article/view/([^/]+)",
                                 journal_path(a["href"]) or "")
                    if m:
                        entry["articles"].append(m.group(1))
                if entry["articles"]:
                    issue["sections"].append(entry)
        except Exception as e:  # noqa: BLE001 - one broken issue must not
            # take down the whole harvest (e.g. after an OJS theme upgrade
            # renames a class this selector relies on).
            print("  issue %s TOC scrape failed, skipping its articles: %s"
                  % (issue["id"], e), file=sys.stderr)
    return issues


def scrape_article(url_path, issue):
    url = JOURNAL + "/article/view/" + url_path
    print("Scraping article %s ..." % url_path)
    doc = soup_of(url)
    # Fall back to the generic <article> inside .page_article if a future
    # OJS theme update drops the .obj_article_details class name.
    det = (doc.select_one(".obj_article_details")
           or doc.select_one(".page_article article"))
    if det is None:
        raise RuntimeError("no article details on %s" % url)

    art = {
        "urlPath": url_path,
        "submissionId": "",
        "title": text_of(det.select_one("h1.page_title") or det.find("h1")),
        "subtitle": text_of(det.select_one("h2.subtitle")),
        "authors": [],
        "doi": "",
        "doiUrl": "",
        "keywords": [],
        "abstractHtml": "",
        "referencesHtml": "",
        "galleys": [],
        "datePublished": "",
        "citation": "",
        "bibtexUrl": "",
        "risUrl": "",
        "issueId": issue["id"],
        "issueTitle": issue["title"],
        "section": "",
        "categories": [],
        "licenseHtml": "",
        "ojsUrl": url,
    }

    for li in det.select(".item.authors ul.authors > li"):
        orcid = li.select_one(".orcid a")
        art["authors"].append({
            "name": text_of(li.select_one(".name")),
            "affiliation": text_of(li.select_one(".affiliation")),
            "orcid": orcid["href"].strip() if orcid else "",
        })

    doi_a = det.select_one(".item.doi .value a")
    if doi_a:
        art["doiUrl"] = doi_a["href"].strip()
        art["doi"] = re.sub(r"^https?://doi\.org/", "", art["doiUrl"])

    kw = det.select_one(".item.keywords .value")
    if kw:
        art["keywords"] = [k.strip() for k in text_of(kw).split(",") if k.strip()]

    abstract = det.select_one(".item.abstract")
    if abstract:
        label = abstract.find("h2")
        if label:
            label.decompose()
        art["abstractHtml"] = clean_fragment(abstract, url)

    refs = det.select_one(".item.references .value")
    if refs:
        art["referencesHtml"] = clean_fragment(refs, url)

    art["datePublished"] = text_of(det.select_one(".item.published .value"))
    citation = det.select_one("#citationOutput .csl-entry")
    art["citation"] = text_of(citation)

    for fmt in ("bibtex", "ris"):
        a = det.select_one('a[href*="citationstylelanguage/download/%s"]' % fmt)
        if a:
            art[fmt + "Url"] = a["href"]
            m = re.search(r"submissionId=(\d+)", a["href"])
            if m:
                art["submissionId"] = m.group(1)

    for sub in det.select(".item.issue .sub_item"):
        label = text_of(sub.find("h2")).lower()
        if "section" in label:
            art["section"] = text_of(sub.select_one(".value"))
    art["categories"] = [text_of(a) for a in det.select(".item.issue .categories a")]

    lic = det.select_one(".item.copyright")
    if lic:
        label = lic.find("h2")
        if label:
            label.decompose()
        art["licenseHtml"] = clean_fragment(lic, url)

    for a in det.select(".item.galleys a.obj_galley_link"):
        href = urllib.parse.urljoin(url, a["href"])
        gpath = journal_path(href) or ""
        m = re.match(r"article/view/([^/]+)/([^/]+)$", gpath)
        if not m:
            continue
        is_pdf = "pdf" in (a.get("class") or []) or text_of(a).upper() == "PDF"
        galley = {
            "label": text_of(a),
            "isPdf": is_pdf,
            "viewUrl": href,
            "downloadUrl": JOURNAL + "/article/download/%s/%s" % m.groups(),
            "localPdf": "",
        }
        if is_pdf:
            galley["localPdf"] = "%s-%s.pdf" % m.groups()
        art["galleys"].append(galley)
    return art


def _team_slug(name):
    """Filename slug for a team member's photo: 'Lukas Röseler' -> 'lukas-roeseler'."""
    import unicodedata
    for de, ascii_ in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"),
                       ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue")):
        name = name.replace(de, ascii_)
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _first_meaningful_child(node):
    """First child of a BeautifulSoup tag that isn't just whitespace text."""
    for c in node.contents:
        if isinstance(c, str) and not c.strip():
            continue
        return c
    return None


def parse_team(page_html):
    """Turn the editorial-team page (p>strong headings + ul lists) into
    structured sections. Every section gets photo circles; members without
    an uploaded photo show an initials placeholder.
    """
    doc = BeautifulSoup(page_html, "html.parser")
    sections = []
    current = None
    for node in doc.find_all(["p", "ul"], recursive=False):
        if node.name == "p":
            strong = node.find("strong")
            if strong and text_of(strong) == text_of(node):
                # The whole paragraph is just the heading, e.g. <p><strong>
                # Former Members</strong></p>.
                current = {"title": text_of(strong), "members": [],
                           "hasPhotos": True, "noteHtml": ""}
                sections.append(current)
            elif strong and _first_meaningful_child(node) is strong:
                # Heading and body text share one paragraph, e.g. <p><strong>
                # Institutional OJS Support</strong><br/>We are grateful...
                # </p>. Split the leading heading off into its own section.
                # This pattern is prose, not a member list, so no photo grid.
                current = {"title": text_of(strong), "members": [],
                           "hasPhotos": False, "noteHtml": ""}
                sections.append(current)
                strong.extract()
                br = _first_meaningful_child(node)
                if br is not None and getattr(br, "name", None) == "br":
                    br.extract()
                current["noteHtml"] += str(node)
            elif current is not None:
                current["noteHtml"] += str(node)
        elif node.name == "ul" and current is not None:
            for li in node.find_all("li", recursive=False):
                text = text_of(li)
                m = (re.match(r"^([^(]+?)\s*\((.*?)\)\s*(?::\s*(.*))?$", text)
                     or re.match(r"^([^(]+?)\s*\((.*)$", text))
                if m:
                    groups = m.groups() + ("",)
                    name = groups[0].strip()
                    affiliation = groups[1].rstrip(") ").strip()
                    roles = (groups[2] or "").strip()
                else:
                    name, affiliation, roles = text, "", ""
                current["members"].append({
                    "name": name,
                    "affiliation": affiliation,
                    "roles": roles,
                    "slug": _team_slug(name),
                })
    return {"sections": [s for s in sections if s["members"] or s["noteHtml"]]}


def scrape_announcements():
    print("Scraping announcements ...")
    index_url = JOURNAL + "/announcement"
    doc = soup_of(index_url)
    items = []
    for summary in doc.select(".obj_announcement_summary"):
        a = summary.find("a", href=True)
        if not a:
            continue
        m = re.match(r"announcement/view/(\d+)$", journal_path(a["href"]) or "")
        if not m:
            continue
        items.append({"id": m.group(1), "title": text_of(a)})

    announcements = []
    for item in items:
        url = JOURNAL + "/announcement/view/" + item["id"]
        doc = soup_of(url)
        full = doc.select_one(".obj_announcement_full")
        if full is None:
            print("  skipping announcement %s (no body)" % item["id"],
                  file=sys.stderr)
            continue
        title = text_of(full.find("h1")) or item["title"]
        date = text_of(full.select_one(".date"))
        desc = full.select_one(".description")
        announcements.append({
            "id": item["id"],
            "title": title,
            "date": date,
            "html": clean_fragment(desc, url) if desc else "",
            "ojsUrl": url,
        })
    return announcements


def mirror_pdfs(articles):
    print("Mirroring PDF galleys ...")
    for art in articles:
        for galley in art["galleys"]:
            if not galley["localPdf"]:
                continue
            target = os.path.join(PDF_DIR, galley["localPdf"])
            try:
                r = get(galley["downloadUrl"])
                if not r.content.startswith(b"%PDF"):
                    raise RuntimeError("%s did not return a PDF"
                                       % galley["downloadUrl"])
            except RuntimeError as e:
                # Don't let one flaky download (or a renamed download route
                # after an OJS upgrade) abort mirroring of every other PDF.
                # A genuinely missing file is still caught below by the
                # final "missing mirrored PDFs" sanity check, which blocks
                # publishing.
                if os.path.exists(target):
                    print("  keeping existing %s (%s)" % (galley["localPdf"], e),
                          file=sys.stderr)
                else:
                    print("  WARNING: could not mirror %s: %s"
                          % (galley["localPdf"], e), file=sys.stderr)
                continue
            if (os.path.exists(target)
                    and os.path.getsize(target) == len(r.content)):
                continue
            with open(target, "wb") as fh:
                fh.write(r.content)
            print("  saved %s (%d kB)" % (galley["localPdf"],
                                          len(r.content) // 1024))


# ---------------------------------------------------------------------------
# Usage statistics via the authenticated OJS API
# ---------------------------------------------------------------------------

def monthly_timeline(sub_id, metric, date_end):
    """{YYYY-MM: count} for metric 'abstract' or 'galley' (from r2d2)."""
    data = api_get("/stats/publications/%s/%s" % (sub_id, metric), {
        "timelineInterval": "month",
        "dateStart": DATE_START,
        "dateEnd": date_end,
    })
    out = {}
    rows = data if isinstance(data, list) else (data or {}).get("items", [])
    for row in rows:
        date = str(row.get("date") or row.get("label") or "")
        value = row.get("value")
        m = re.match(r"(\d{4})-(\d{2})", date)
        if not m or value in (None, ""):
            continue
        ym = "%s-%s" % (m.group(1), m.group(2))
        try:
            out[ym] = out.get(ym, 0) + int(value)
        except (TypeError, ValueError):
            continue
    return out


def fetch_stats(articles):
    """Per-article usage stats: OJS abstract-page views, PDF downloads (both
    via the authenticated OJS API), and mirror-page views (via GoatCounter,
    see fetch_goatcounter_views). Kept as three distinct numbers rather than
    merged, since they measure different things: "views on OJS" only counts
    someone loading the abstract page on ejournals.uni-muenster.de itself
    (a mirror visitor never triggers that), while "views on this mirror"
    comes from GoatCounter tracking this site. Downloads are accurate on
    either count, since the mirror's Download PDF button links straight to
    the canonical OJS download URL, which OJS counts either way.
    """
    stats = {}
    if not API_KEY:
        print("OJS_API_KEY not set - skipping OJS usage statistics.")
    else:
        import datetime
        date_end = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        print("Fetching usage statistics via OJS API ...")
        if api_get("/stats/publications", {"count": 1}) is None:
            print("Stats preflight failed - skipping OJS usage statistics.",
                  file=sys.stderr)
        else:
            for art in articles:
                sid = art["submissionId"]
                if not sid:
                    continue
                views = monthly_timeline(sid, "abstract", date_end)
                downloads = monthly_timeline(sid, "galley", date_end)
                stats[sid] = {
                    "ojsViews": sum(views.values()),
                    "monthlyOjsViews": views,
                    "pdfDownloads": sum(downloads.values()),
                    "monthlyDownloads": downloads,
                    "asOf": date_end,
                }
                print("  %s: %d OJS views, %d downloads"
                      % (sid, stats[sid]["ojsViews"], stats[sid]["pdfDownloads"]))

    goatcounter_views = fetch_goatcounter_views(articles)
    for art in articles:
        sid = art["submissionId"]
        views = goatcounter_views.get(art["urlPath"])
        if views is None:
            continue
        stats.setdefault(sid, {})["mirrorViews"] = views

    return stats


# ---------------------------------------------------------------------------
# Mirror page views via GoatCounter (https://www.goatcounter.com), a free,
# cookie-less, privacy-friendly analytics service. The tracking snippet is
# in templates/base.html; this just reads the counts back out at build time.
# ---------------------------------------------------------------------------

GOATCOUNTER_SITE = os.environ.get("GOATCOUNTER_SITE", "replicationresearch").strip()
GOATCOUNTER_TOKEN = os.environ.get("GOATCOUNTER_API_TOKEN", "").strip()


def _site_base_path():
    """The path GitHub Pages serves this site under, e.g. '/r2/' - needed
    because GoatCounter records the full pathname visitors see, including
    that prefix. Mirrors build.py's BASE_URL normalisation.
    """
    base = os.environ.get("BASE_URL", "/r2/").strip() or "/r2/"
    if not base.startswith("/"):
        base = "/" + base
    if not base.endswith("/"):
        base += "/"
    return base


def fetch_goatcounter_views(articles):
    """{urlPath: view count} for every article's mirror page, or {} if the
    GOATCOUNTER_API_TOKEN secret isn't configured yet (skip, don't fail).
    """
    if not GOATCOUNTER_TOKEN:
        print("GOATCOUNTER_API_TOKEN not set - skipping mirror-view stats.")
        return {}
    base_path = _site_base_path()
    paths = {art["urlPath"]: base_path + "articles/%s/" % art["urlPath"]
             for art in articles}
    if not paths:
        return {}
    print("Fetching mirror page views from GoatCounter ...")
    url = "https://%s.goatcounter.com/api/v0/stats/hits" % GOATCOUNTER_SITE
    headers = {"Authorization": "Bearer " + GOATCOUNTER_TOKEN,
               "Content-Type": "application/json"}
    try:
        r = session.get(url, headers=headers, timeout=30, params={
            "include_paths": list(paths.values()),
            "path_by_name": "true",
            "start": DATE_START + "T00:00:00Z",
            "limit": 100,
        })
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001 - analytics must never block a build
        print("  GoatCounter fetch failed, skipping: %s" % e, file=sys.stderr)
        return {}
    by_full_path = {hit.get("path"): hit.get("count", 0)
                    for hit in data.get("hits", [])}
    result = {}
    for url_path, full_path in paths.items():
        if full_path in by_full_path:
            result[url_path] = by_full_path[full_path]
    print("  got mirror views for %d/%d articles" % (len(result), len(paths)))
    return result


# ---------------------------------------------------------------------------
# Submission funnel (for the Submissions page charts): how many manuscripts
# came in per month, and how they broke down (published / under review /
# declined). Uses the same OJS_API_KEY as the stats above.
# ---------------------------------------------------------------------------

# pkp-lib submission status constants (stable since OJS 3.0).
STATUS_PUBLISHED = 3
STATUS_DECLINED = 4


def fetch_submission_stats():
    if not API_KEY:
        print("OJS_API_KEY not set - skipping submission-funnel stats.")
        return None
    print("Fetching submission list via OJS API ...")
    items = []
    offset = 0
    while True:
        data = api_get("/submissions", {"count": 100, "offset": offset})
        if not data:
            break
        batch = data.get("items") or []
        items.extend(batch)
        total = data.get("itemsMax", 0)
        offset += len(batch)
        if not batch or offset >= total:
            break
    if not items:
        print("  no submissions returned - skipping.", file=sys.stderr)
        return None

    monthly = {}
    counts = {"published": 0, "underReview": 0, "declined": 0}
    for item in items:
        date = str(item.get("dateSubmitted") or "")[:7]
        m = re.match(r"\d{4}-\d{2}$", date)
        if m:
            monthly[date] = monthly.get(date, 0) + 1
        status = item.get("status")
        if status == STATUS_PUBLISHED:
            counts["published"] += 1
        elif status == STATUS_DECLINED:
            counts["declined"] += 1
        else:
            counts["underReview"] += 1
    print("  %d submissions: %d published, %d under review, %d declined"
          % (len(items), counts["published"], counts["underReview"],
             counts["declined"]))
    return {"monthly": monthly, "statusCounts": counts, "total": len(items)}


# ---------------------------------------------------------------------------
# "Under Review" home-page list: manually maintained by the editors in a
# Google Sheet (view-only link), read back via its CSV export - no API key
# needed since it's shared as "anyone with the link can view". Author names
# for entries with a preprint DOI are resolved via Crossref; entries without
# a DOI fall back to the sheet's own "First Author" column.
# ---------------------------------------------------------------------------

SHEET_ID = "1aayvi3FR25suhT_DNPQXsef6YObErgIF1iGP6OS1R-w"
SHEET_TAB = "Submissions"
SHEET_CSV_URL = ("https://docs.google.com/spreadsheets/d/%s/gviz/tq"
                  "?tqx=out:csv&sheet=%s" % (SHEET_ID, SHEET_TAB))
CROSSREF_MAILTO = "contact@replicationresearch.org"


def _unescape_fully(s):
    """Repeatedly HTML-unescape until stable.

    Some publishers double-encode metadata before it reaches Crossref, so a
    title can arrive as literal text containing "&amp;amp;" (i.e. already
    escaped once). A single html.unescape() only resolves one layer,
    leaving "&amp;" as visible text; looping to a fixed point resolves any
    depth of accidental double-encoding.
    """
    import html
    prev = None
    while prev != s:
        prev = s
        s = html.unescape(s)
    return s


def _doi_lookup_get(url, params=None):
    """GET for an optional DOI-metadata lookup. A 404 here is a real,
    final answer ("this registration agency doesn't have this DOI"), not
    a transient failure - unlike fetch.py's other requests, it must not
    be retried as if it were one. Only actual network/server errors get a
    couple of quick retries.
    """
    last = None
    for attempt in range(2):
        _throttle()
        try:
            r = session.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            last = str(e)
            time.sleep(2)
            continue
        if r.status_code == 200:
            return r
        if r.status_code == 404:
            return None
        last = "HTTP %s" % r.status_code
        time.sleep(2)
    if last:
        print("  DOI metadata lookup failed for %s: %s" % (url, last),
              file=sys.stderr)
    return None


def _crossref_lookup(doi):
    r = _doi_lookup_get("https://api.crossref.org/works/%s" % doi,
                        {"mailto": CROSSREF_MAILTO})
    if r is None:
        return None
    msg = r.json().get("message", {})
    title = _unescape_fully((msg.get("title") or [""])[0].strip())
    authors = []
    for a in msg.get("author") or []:
        name = " ".join(p for p in (a.get("given"), a.get("family")) if p)
        name = _unescape_fully(name)
        if name:
            authors.append({"name": name, "affiliation": "",
                             "orcid": a.get("ORCID") or ""})
    if not title and not authors:
        return None
    return {"title": title, "authors": authors}


def _datacite_lookup(doi):
    """Crossref doesn't carry arXiv (or some other preprint servers') DOIs
    - those are registered with DataCite instead. Same {title, authors}
    shape as _crossref_lookup so callers don't need to care which agency
    actually had it.
    """
    r = _doi_lookup_get("https://api.datacite.org/dois/%s" % doi)
    if r is None:
        return None
    attrs = (r.json().get("data") or {}).get("attributes", {})
    titles = attrs.get("titles") or []
    title = _unescape_fully((titles[0].get("title") if titles else "") or "")
    authors = []
    for c in attrs.get("creators") or []:
        name = (" ".join(p for p in (c.get("givenName"), c.get("familyName"))
                         if p) or c.get("name") or "")
        name = _unescape_fully(name)
        if not name:
            continue
        orcid = ""
        for nid in c.get("nameIdentifiers") or []:
            if (nid.get("nameIdentifierScheme") or "").upper() == "ORCID":
                orcid = nid.get("nameIdentifier") or ""
                break
        authors.append({"name": name, "affiliation": "", "orcid": orcid})
    if not title and not authors:
        return None
    return {"title": title, "authors": authors}


def _doi_metadata(doi):
    """{title, authors} for a DOI via Crossref, falling back to DataCite,
    or None if neither registration agency has it (the sheet's own
    GDrive-folder title / first-author column is the final fallback,
    handled by the caller).
    """
    return _crossref_lookup(doi) or _datacite_lookup(doi)


def _parse_sheet_date(raw):
    """'15.11.2025' -> '2025-11-15'; keeps the raw string if unparseable."""
    import datetime
    try:
        return datetime.datetime.strptime(raw.strip(), "%d.%m.%Y").date().isoformat()
    except (ValueError, AttributeError):
        return raw.strip()


def _prereview_url(doi):
    """PREreview's write-a-prereview URL for a DOI, e.g.
    '10.31234/osf.io/5e8mz_v1' -> '.../doi-10.31234-osf.io-5e8mz_v1/...'
    - PREreview slugs any preprint DOI (OSF, arXiv, bioRxiv, ...) by
    lowercasing it, prefixing "doi-", and replacing every "/" with "-".
    Lowercasing matters: DOIs are case-insensitive, but PREreview's slug
    matching isn't, and it stores them lowercase (e.g. "arXiv" -> "arxiv").
    """
    slug = "doi-" + doi.lower().replace("/", "-")
    return "https://prereview.org/preprints/%s/write-a-prereview" % slug


def fetch_under_review():
    """Submissions still active in review, for the home page's "Under
    Review" card. Only rows where the editors have filled in "Stage
    website" are shown - that column is the explicit public-facing status,
    kept separate from the free-text internal "Stage" notes column so
    nothing gets published before an editor has actually chosen to.
    """
    import csv
    import io

    print("Fetching Under Review list from the editors' Google Sheet ...")
    try:
        r = get(SHEET_CSV_URL)
        rows = list(csv.DictReader(io.StringIO(r.text)))
    except Exception as e:  # noqa: BLE001
        print("  sheet fetch failed: %s" % e, file=sys.stderr)
        return None

    # Belt-and-braces: even though "Stage website" is meant to be curated
    # specifically for public display, don't show something under "Under
    # Review" if the editors' own status text says it's actually finished.
    terminal = re.compile(
        r"\b(published|accepted|withdrawn|declined|rejected|desk reject)\b",
        re.IGNORECASE)

    entries = []
    for row in rows:
        status = (row.get("Stage website") or "").strip()
        sub_id = (row.get("ID") or "").strip()
        if not status or not sub_id.isdigit() or terminal.search(status):
            continue

        folder = (row.get("GDrive Folder") or "").strip().strip("—-")
        fallback_title = re.sub(r"^\d+\s*", "", folder).strip() or folder
        first_author = (row.get("First Author") or "").strip()

        doi_url = (row.get("Preprint") or "").strip()
        doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi_url)
        doi_meta = _doi_metadata(doi) if doi else None
        if doi and doi_meta is None:
            print("  no Crossref/DataCite record for preprint DOI %s "
                  "(submission %s) - check it resolves at https://doi.org/%s"
                  % (doi, sub_id, doi), file=sys.stderr)

        entries.append({
            "id": sub_id,
            "title": (doi_meta or {}).get("title") or fallback_title
                     or "Untitled submission",
            "authors": (doi_meta or {}).get("authors")
                       or ([{"name": first_author, "affiliation": "", "orcid": ""}]
                           if first_author else []),
            "preprintUrl": doi_url,
            "prereviewUrl": _prereview_url(doi) if doi else "",
            "status": status,
            "handlingEditor": (row.get("Handling Editor") or "").strip(),
            "submissionDate": _parse_sheet_date(row.get("Submission Date") or ""),
        })

    entries.sort(key=lambda e: e["submissionDate"], reverse=True)
    print("  %d submissions listed as under review" % len(entries))
    return entries


def _bare_doi(value):
    return re.sub(r"^https?://(dx\.)?doi\.org/", "", (value or "").strip(),
                  flags=re.IGNORECASE).lower()


def _doi_or_url(value):
    """A sheet cell may hold a bare DOI or a full https://doi.org/... link -
    normalize either into a clickable URL."""
    value = (value or "").strip()
    if not value:
        return ""
    return value if value.lower().startswith("http") \
        else "https://doi.org/" + value


def fetch_published_extras():
    """Peer Review Report / Reproducibility Certificate links for published
    articles, keyed by the article's own (published) DOI. Editors fill in
    the "Published DOI", "Peer Review Report", and "Repro Cert" columns on
    the same tracking sheet once a submission is accepted; editorial
    communications never get these filled in, so they never get the extra
    buttons on the article page.
    """
    import csv
    import io

    print("Fetching peer-review/repro-cert links from the editors' sheet ...")
    try:
        r = get(SHEET_CSV_URL)
        rows = list(csv.DictReader(io.StringIO(r.text)))
    except Exception as e:  # noqa: BLE001
        print("  sheet fetch failed: %s" % e, file=sys.stderr)
        return None

    extras = {}
    for row in rows:
        doi = _bare_doi(row.get("Published DOI"))
        review_url = _doi_or_url(row.get("Peer Review Report"))
        cert_url = _doi_or_url(row.get("Repro Cert"))
        if doi and (review_url or cert_url):
            extras[doi] = {"peerReviewUrl": review_url, "reproCertUrl": cert_url}

    print("  %d published article(s) with review/certificate links"
          % len(extras))
    return extras


# ---------------------------------------------------------------------------

def write_json(name, payload):
    path = os.path.join(DATA_DIR, name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    print("Wrote data/%s" % name)


def main():
    for d in (DATA_DIR, PDF_DIR, IMG_DIR):
        os.makedirs(d, exist_ok=True)

    # Every scrape_* call below is isolated with try/except: a single broken
    # selector (e.g. after OJS is upgraded and a theme class gets renamed)
    # should degrade that one item, not crash the whole harvest. The sanity
    # checks further down are what actually block publishing, by looking at
    # the aggregate result rather than any individual exception.
    journal = scrape_journal()
    page_paths = discover_page_paths(journal["nav"])
    PAGE_SLUGS.update(page_paths)

    try:
        issues = scrape_issues()
    except Exception as e:  # noqa: BLE001
        print("issue archive scrape failed entirely: %s" % e, file=sys.stderr)
        issues = []

    articles = []
    for issue in issues:
        for section in issue["sections"]:
            for url_path in section["articles"]:
                try:
                    articles.append(scrape_article(url_path, issue))
                except Exception as e:  # noqa: BLE001
                    print("  article %s scrape failed, skipping: %s"
                          % (url_path, e), file=sys.stderr)

    pages = []
    for path in page_paths:
        try:
            pages.append(scrape_page(path))
        except Exception as e:  # noqa: BLE001
            print("  page /%s failed: %s" % (path, e), file=sys.stderr)

    team = {"sections": []}
    for page in pages:
        if page["slug"] == "about/editorialTeam":
            try:
                team = parse_team(page["html"])
            except Exception as e:  # noqa: BLE001
                print("  editorial team parse failed: %s" % e, file=sys.stderr)
            if not team["sections"]:
                print("  editorial team page did not parse into sections; "
                      "the generic page layout will be used", file=sys.stderr)

    try:
        announcements = scrape_announcements()
    except Exception as e:  # noqa: BLE001
        print("announcements scrape failed entirely: %s" % e, file=sys.stderr)
        announcements = []

    mirror_pdfs(articles)
    stats = fetch_stats(articles)
    submission_stats = fetch_submission_stats()
    under_review = fetch_under_review()
    published_extras = fetch_published_extras()

    # Sanity checks: never publish an obviously broken harvest.
    problems = []
    if not issues:
        problems.append("no issues found")
    if not articles:
        problems.append("no articles found")
    if any(not a["title"] for a in articles):
        problems.append("article without title")
    if len(pages) < 3:
        problems.append("only %d static pages scraped" % len(pages))
    pdf_files = [g["localPdf"] for a in articles for g in a["galleys"]
                 if g["localPdf"]]
    missing = [p for p in pdf_files
               if not os.path.exists(os.path.join(PDF_DIR, p))]
    if missing:
        problems.append("missing mirrored PDFs: %s" % ", ".join(missing))
    if problems:
        print("HARVEST FAILED: " + "; ".join(problems), file=sys.stderr)
        sys.exit(1)

    write_json("journal.json", journal)
    write_json("issues.json", issues)
    write_json("articles.json", articles)
    write_json("pages.json", pages)
    write_json("team.json", team)
    write_json("announcements.json", announcements)
    if stats or not os.path.exists(os.path.join(DATA_DIR, "stats.json")):
        write_json("stats.json", stats)
    else:
        print("Keeping previous stats.json (stats fetch unavailable).")
    if submission_stats is not None:
        write_json("submissions.json", submission_stats)
    else:
        print("Keeping previous submissions.json (fetch unavailable).")
    if under_review is not None:
        write_json("under_review.json", under_review)
    else:
        print("Keeping previous under_review.json (fetch unavailable).")
    if published_extras is not None:
        write_json("published_extras.json", published_extras)
    else:
        print("Keeping previous published_extras.json (fetch unavailable).")

    print("Done: %d issues, %d articles, %d pages, %d announcements, %d PDFs."
          % (len(issues), len(articles), len(pages), len(announcements),
             len(pdf_files)))


if __name__ == "__main__":
    main()
