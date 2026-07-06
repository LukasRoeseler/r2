# Replication Research (R2) — Mirror Site

A beautiful, static, self-updating mirror of the journal
[*Replication Research*](https://ejournals.uni-muenster.de/index.php/replicationresearch/index)
(OJS), hosted on GitHub Pages.

- **No manual content entry.** A GitHub Action fetches everything from OJS
  daily — issues, articles, abstracts, static pages, announcements, PDFs, and
  usage statistics — and redeploys the site.
- **Submit manuscript** buttons lead to the OJS submission portal.
- **View PDF** leads to the canonical PDF on OJS; each article page also embeds
  a [pdf.js](https://mozilla.github.io/pdf.js/) viewer (zoom, search, paging)
  that renders a mirrored, same-origin copy of the PDF.
- **Usage statistics** (abstract views, PDF downloads) come from the
  authenticated OJS REST API — the same `OJS_API_KEY` pattern as the
  [r2d2 dashboard](https://github.com/LukasRoeseler/r2d2).

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

1. Create the GitHub repository (`LukasRoeseler/r2`) and push this folder
   to `main`. (The workflow derives `BASE_URL` from the repository name, so
   a rename only requires updating the defaults in `scripts/build.py` and
   `scripts/serve.py` for local use.)
2. Add the OJS API key: **Settings → Secrets and variables → Actions →
   New repository secret**, name `OJS_API_KEY` (same token as in r2d2;
   from OJS: User Profile → API Key). Without it the site still builds,
   just without the usage-statistics boxes.
3. Enable Pages: **Settings → Pages → Build and deployment → Source:
   GitHub Actions**.
4. Run the workflow once: **Actions → "Fetch content and deploy mirror" →
   Run workflow**. After that it runs daily at 04:23 UTC.

## Local development

```bash
pip install -r requirements.txt
python scripts/fetch.py    # optional: put OJS_API_KEY=... in a .env file first
python scripts/build.py
python scripts/serve.py    # -> http://localhost:8737/r2/
```

## Custom domain later

Set `BASE_URL: /` in `.github/workflows/build.yml`, add the domain under
**Settings → Pages → Custom domain** (GitHub then serves a CNAME), and point
the domain's DNS at GitHub Pages.

## Notes

- GitHub disables scheduled workflows after ~60 days without repository
  activity. The daily statistics commit usually counts as activity; if the
  journal *and* its stats go quiet for two months, re-enable the workflow from
  the Actions tab.
- pdf.js is pinned (v6.1.200, `static/pdfjs/`, sourcemaps and unused locales
  removed). To upgrade, download a newer `pdfjs-*-legacy-dist.zip` from the
  [pdf.js releases](https://github.com/mozilla/pdf.js/releases) and repeat the
  trim.
