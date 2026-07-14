# Replication Research (R2) — Mirror Site

A beautiful, static, self-updating mirror of the journal
[*Replication Research*](https://ejournals.uni-muenster.de/index.php/replicationresearch/index)
(OJS), hosted on GitHub Pages.

- **No manual content entry.** A GitHub Action fetches everything from OJS
  daily — issues, articles, abstracts, static pages, announcements, PDFs, and
  usage statistics — and redeploys the site.
- **Submit manuscript** buttons lead to the OJS submission portal.
- **Download PDF** leads to the canonical PDF download URL on OJS (so OJS's
  own download counter still increments); each article page also embeds a
  [pdf.js](https://mozilla.github.io/pdf.js/) viewer (zoom, search, paging)
  that renders a mirrored, same-origin copy of the PDF for in-page reading.
- **Usage statistics** combine two sources, kept separate because they
  measure different things: OJS abstract-page views and PDF downloads come
  from the authenticated OJS REST API (`OJS_API_KEY`, same pattern as the
  [r2d2 dashboard](https://github.com/LukasRoeseler/r2d2)) and only count
  activity on ejournals.uni-muenster.de itself; views of the *mirror's own*
  pages come from [GoatCounter](https://www.goatcounter.com) (free, no
  cookies), tracked via the snippet in `templates/base.html` and read back
  through its API (`GOATCOUNTER_API_TOKEN`).
- **"Under Review"** on the home page lists submissions still active in
  review. The editors maintain this themselves in a Google Sheet (view-only
  link, read via its CSV export - no credentials needed); a row only shows
  up once its "Stage website" column is filled in, so nothing goes public
  before an editor chooses to. Titles/authors come from Crossref when a
  preprint DOI is given, else fall back to the sheet's own columns.

## How it works

```
scripts/fetch.py   scrapes OJS + calls its API  ->  data/*.json, assets/pdf, assets/img
scripts/build.py   Jinja2 templates             ->  _site/  (deployed to Pages)
scripts/serve.py   local dev server (simulates the GitHub Pages subpath)
```

`fetch.py` refuses to write (exits non-zero) if the harvest looks broken, so a
temporary OJS outage never wipes the published site. Harvested HTML stores the
site root as `__BASE__`; `build.py` substitutes the real base path, so moving
to a custom domain later only means changing `BASE_URL`.

## One-time setup

1. Create the GitHub repository (currently
   [`replicationresearch/replicationresearch.github.io`](https://github.com/replicationresearch/replicationresearch.github.io))
   and push this folder to `main`. The workflow derives `BASE_URL` from the
   repository name, root ("/") for a `<user-or-org>.github.io` repo name and
   `/<repo>/` otherwise, so a rename only needs updating the defaults in
   `scripts/build.py` and `scripts/serve.py` for local use if the new name
   crosses that boundary.
2. Add the OJS API key: **Settings → Secrets and variables → Actions →
   New repository secret**, name `OJS_API_KEY` (same token as in r2d2;
   from OJS: User Profile → API Key). Without it the site still builds,
   just without OJS views/downloads or the submission-funnel charts.
3. Optional, for mirror-page view counts: create a free site at
   [goatcounter.com](https://www.goatcounter.com) (site code
   `replicationresearch` is already wired into `templates/base.html` — change
   it there if you use a different code), then **Settings → API** in the
   GoatCounter dashboard to create a token with read access to stats, and add
   it as the `GOATCOUNTER_API_TOKEN` repository secret. Without it, mirror
   views are simply omitted (OJS views and downloads still show).
4. Enable Pages: **Settings → Pages → Build and deployment → Source:
   GitHub Actions**.
5. Run the workflow once: **Actions → "Fetch content and deploy mirror" →
   Run workflow**. After that it runs daily at 04:23 UTC.

## Local development

```bash
pip install -r requirements.txt
python scripts/fetch.py    # optional: put OJS_API_KEY=... in a .env file first
python scripts/build.py
python scripts/serve.py    # -> http://localhost:8737/
```

## Custom domain later

`BASE_URL` is already root ("/") for this repo, so just add the domain under
**Settings → Pages → Custom domain** (GitHub then serves a CNAME) and point
the domain's DNS at GitHub Pages.

## Citable snapshot for Zenodo

**Actions → "Build Zenodo snapshot export" → Run workflow** builds the site,
then flattens every issue/article/announcement/page into one self-contained
`r2-mirror-snapshot-<date>.html` file (CSS and images inlined, no companion
folder) and attaches it to a new GitHub Release (tagged `zenodo-export-<date>`)
— run it whenever you want a new, dated, citable version. Download it from
the release and upload it to Zenodo yourself, or connect this repo to
[Zenodo's GitHub integration](https://docs.github.com/en/repositories/archiving-a-github-repository/referencing-and-citing-content)
once, and every release you create this way becomes a new Zenodo version
automatically.

Open the file directly (double-click, no server needed) to browse it before
uploading. Each section is labelled with its original path on the live site
(e.g. `/articles/9577`), and the table of contents at the top links to every
one via a same-document fragment — so a specific page inside an archived
snapshot can be cited or linked to directly, e.g.
`r2-mirror-snapshot-2026-07-10.html#articles/9577`.

`scripts/assemble_snapshot.py` does the flattening: it reads the multi-file
`_site/` build (root-relative links, one directory per page — the same
output `scripts/build.py` produces for the live GitHub Pages deploy) and
combines it into the single file, rewriting internal links to fragment
anchors and inlining local images/CSS as it goes.

## Notes

- GitHub disables scheduled workflows after ~60 days without repository
  activity. The daily statistics commit usually counts as activity; if the
  journal *and* its stats go quiet for two months, re-enable the workflow from
  the Actions tab.
- pdf.js is pinned (v6.1.200, `static/pdfjs/`, sourcemaps and unused locales
  removed). To upgrade, download a newer `pdfjs-*-legacy-dist.zip` from the
  [pdf.js releases](https://github.com/mozilla/pdf.js/releases) and repeat the
  trim.
- **OJS upgrade resilience.** `fetch.py` scrapes OJS's default-theme HTML,
  whose class names (`.obj_article_details`, `.obj_issue_toc`, ...) have been
  stable across OJS 3.1–3.3 and are expected to keep working after an upgrade
  to 3.4/3.5. To be safe regardless, every scrape call is isolated with
  try/except: one broken selector degrades just that issue, article, or page
  (logged to the workflow run's output) instead of crashing the whole
  harvest, and a couple of the most load-bearing selectors (article title,
  article container) have a generic-HTML fallback. The site is only ever
  refused a publish if the *aggregate* result looks broken (no issues, no
  articles, missing PDFs) — see the sanity checks at the end of
  `fetch.py`'s `main()`. If OJS is upgraded and something still looks off,
  check the failed run's log for which selector stopped matching.
- **If a scheduled run fails**, the job stops before the commit/build/deploy
  steps, so the currently published site is left exactly as it was — nothing
  gets overwritten with broken or partial data. You'll also get a heads-up
  two ways: GitHub's own "workflow run failed" email (on by default for repo
  owners; check **your GitHub notification settings → Actions** if you don't
  want to rely on it), and a repository issue titled "Mirror build failed"
  that the workflow opens automatically (and closes again once a later run
  succeeds), in case the email gets missed or filtered.
