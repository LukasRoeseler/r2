"""Extract an article PDF's body as HTML for the article page's Full Text
section: structured text (headings from bookmarks/bold spans, de-hyphenated
paragraphs), figures rendered to PNG (caption-anchored clip rendering, so
vector charts survive), and display equations rendered to PNG.

Everything here is best-effort on purpose: extraction must never break a
build, so extract_fulltext() swallows its own failures and returns empty
results, leaving the article page with just the pdf.js viewer as before.
"""

import html
import re
import sys

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

# Placed images smaller than this are page furniture (ORCID icons, logos,
# license badges), not figures.
MIN_FIG_W, MIN_FIG_H = 100, 60

CAPTION_RE = re.compile(r"^(Figure|Fig\.|Table)\s*\d+", re.IGNORECASE)
PAGE_NUM_RE = re.compile(r"^\d{1,3}$")
MATH_FONT_RE = re.compile(r"CM[A-Z]|Math|Symbol|MSAM|MSBM|MJX")
BOLD_FLAG = 16

# The reference list is scraped from OJS and shown in its own section on the
# article page, so the PDF's copy is skipped rather than duplicated.
REFERENCE_HEADINGS = {"references", "bibliography", "literature", "literatur"}


def _norm(text):
    return re.sub(r"\s+", " ", text).strip().lower()


def _furniture_key(text):
    """Normalization for header/footer detection: running footers often
    carry the page number inside the same block ("... | 7"), which would
    make every page's footer unique - strip leading/trailing digits and
    separator punctuation before comparing."""
    t = _norm(text)
    t = re.sub(r"^[\s\d|·•—–-]+", "", t)
    t = re.sub(r"[\s\d|·•—–-]+$", "", t)
    return t


def _block_text(block):
    """Join a text block's lines into one de-hyphenated paragraph string."""
    text = ""
    for line in block["lines"]:
        piece = "".join(span["text"] for span in line["spans"]).strip()
        if not piece:
            continue
        if text.endswith("-") and text[-2:-1].isalpha() and piece[:1].islower():
            text = text[:-1] + piece       # re-join a line-break hyphenation
        elif text:
            text += " " + piece
        else:
            text = piece
    return re.sub(r"\s+", " ", text).strip()


def _span_stats(block):
    """(dominant size, dominant-bold?, math-char share) for a text block."""
    by_style = {}
    math_chars = total_chars = 0
    for line in block["lines"]:
        for span in line["spans"]:
            n = len(span["text"].strip())
            if not n:
                continue
            key = (round(span["size"], 1), bool(span["flags"] & BOLD_FLAG))
            by_style[key] = by_style.get(key, 0) + n
            total_chars += n
            if MATH_FONT_RE.search(span["font"]):
                math_chars += n
    if not by_style:
        return 0.0, False, 0.0
    (size, bold), _ = max(by_style.items(), key=lambda kv: kv[1])
    return size, bold, math_chars / total_chars


def _body_font_size(doc):
    sizes = {}
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    key = round(span["size"], 1)
                    sizes[key] = sizes.get(key, 0) + len(span["text"])
    return max(sizes.items(), key=lambda kv: kv[1])[0] if sizes else 11.0


def _furniture_texts(doc):
    """Normalized texts of running headers/footers: any block text repeated
    on 3+ pages is page furniture, not content."""
    counts = {}
    for page in doc:
        seen = set()
        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue
            t = _furniture_key(_block_text(block))
            if t and t not in seen:
                seen.add(t)
                counts[t] = counts.get(t, 0) + 1
    return {t for t, n in counts.items() if n >= 3}


def _render_clip(page, rect, zoom=2.0):
    rect = rect & page.rect            # never render outside the page
    if rect.is_empty or rect.width < 20 or rect.height < 15:
        return None
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=rect)
    return pix.tobytes("png")


def _figure_rect_above(page, caption_rect, text_blocks, furniture):
    """The figure region belonging to a caption: image blocks and larger
    vector drawings in the band between the caption's top and the bottom of
    the nearest body-text block above it."""
    top_limit = 0.0
    for tb in text_blocks:
        t = _furniture_key(_block_text(tb))
        if not t or t in furniture:
            continue
        if tb["bbox"][3] <= caption_rect.y0 - 4 and tb["bbox"][3] > top_limit:
            # a text block fully above the caption; but only count it as the
            # band's upper bound if it is NOT part of the figure itself
            # (axis labels are small blocks) - use width as a cheap proxy:
            if (tb["bbox"][2] - tb["bbox"][0]) > page.rect.width * 0.5:
                top_limit = tb["bbox"][3]
    band = fitz.Rect(page.rect.x0, top_limit, page.rect.x1, caption_rect.y0)

    pieces = []
    for block in page.get_text("dict")["blocks"]:
        if block["type"] == 1 and fitz.Rect(block["bbox"]).intersects(band):
            pieces.append(fitz.Rect(block["bbox"]))
    for drawing in page.get_drawings():
        r = drawing["rect"]
        if r.width >= 40 and r.height >= 25 and r.intersects(band):
            pieces.append(r)
    if not pieces:
        return None
    rect = pieces[0]
    for r in pieces[1:]:
        rect |= r
    rect = rect & band
    if rect.width < MIN_FIG_W or rect.height < MIN_FIG_H:
        return None
    return rect + (-4, -4, 4, 4)       # a little breathing room


def _figure_html(url, caption):
    cap = ('<figcaption>%s</figcaption>' % html.escape(caption)) if caption else ""
    alt = html.escape(caption[:80]) if caption else "Figure"
    return ('<figure class="fulltext-figure">'
            '<button class="figure-zoom" type="button" aria-label="Enlarge figure">'
            '<img src="%s" loading="lazy" alt="%s"></button>%s</figure>'
            % (url, alt, cap))


def extract_fulltext(pdf_path, fig_url_prefix):
    """{"html": str, "figures": [(filename, png_bytes), ...]}.

    fig_url_prefix is prepended to figure file names in <img src> URLs,
    e.g. "/r2/assets/fulltext/9577-"; the returned filenames carry the same
    suffixes ("fig-1.png", ...) for the caller to write to disk.
    """
    if fitz is None:
        return {"html": "", "figures": []}
    try:
        result = _extract(pdf_path, fig_url_prefix, ignore_toc=False)
        if not result["html"]:
            # Bookmarked start heading never matched a text block - retry
            # from the top of the document instead of returning nothing.
            result = _extract(pdf_path, fig_url_prefix, ignore_toc=True)
        return result
    except Exception as e:  # noqa: BLE001 - never break the site build
        print("  full-text extraction failed for %s: %s" % (pdf_path, e),
              file=sys.stderr)
        return {"html": "", "figures": []}


def _extract(pdf_path, fig_url_prefix, ignore_toc):
    doc = fitz.open(pdf_path)
    body_size = _body_font_size(doc)
    furniture = _furniture_texts(doc)
    toc = [] if ignore_toc else doc.get_toc()
    toc_titles = {_norm(t[1]): t[0] for t in toc}
    first_heading = _norm(toc[0][1]) if toc else None

    parts = []
    figures = []
    started = first_heading is None    # no bookmarks -> include everything
    in_references = False
    fig_n = 0

    for page in doc:
        d = page.get_text("dict")
        text_blocks = [b for b in d["blocks"] if b["type"] == 0]

        # Pass 1: find captions and their figure regions, and standalone
        # large images, so pass 2 can skip any text living inside a figure
        # (axis labels, legends) - those are already part of the rendering.
        fig_rects = {}                 # caption block index -> clip rect
        for i, block in enumerate(text_blocks):
            text = _block_text(block)
            if CAPTION_RE.match(text):
                rect = _figure_rect_above(page, fitz.Rect(block["bbox"]),
                                          text_blocks, furniture)
                if rect is not None:
                    fig_rects[i] = rect

        standalone = []
        for block in d["blocks"]:
            if block["type"] != 1:
                continue
            rect = fitz.Rect(block["bbox"])
            if rect.width < MIN_FIG_W or rect.height < MIN_FIG_H:
                continue
            if any(rect.intersects(r) for r in fig_rects.values()):
                continue
            standalone.append(rect + (-4, -4, 4, 4))

        # Pass 2: emit content in reading order.
        for i, block in enumerate(text_blocks):
            text = _block_text(block)
            norm = _norm(text)
            if not norm or _furniture_key(text) in furniture \
                    or PAGE_NUM_RE.match(text):
                continue

            size, bold, math_share = _span_stats(block)
            is_heading = ((bold and size >= body_size + 1)
                          or norm in toc_titles) and len(text) < 120 \
                and text[:1] not in "•‣▪✦◦·*–—-" \
                and not text.rstrip().endswith(".")   # sentences aren't headings

            if not started:
                if is_heading and norm == first_heading:
                    started = True
                else:
                    continue

            if is_heading:
                in_references = norm in REFERENCE_HEADINGS
            if in_references:
                continue               # the page shows OJS's reference list

            rect = fitz.Rect(block["bbox"])
            if i in fig_rects:
                png = _render_clip(page, fig_rects[i])
                if png:
                    fig_n += 1
                    name = "fig-%d.png" % fig_n
                    figures.append((name, png))
                    parts.append(_figure_html(fig_url_prefix + name, text))
                    continue
                # fall through: keep the caption as plain text

            if any(rect.intersects(r) for r in fig_rects.values()):
                continue               # text inside a rendered figure

            if math_share > 0.5 and not is_heading:
                png = _render_clip(page, rect + (-2, -2, 2, 2))
                if png:
                    fig_n += 1
                    name = "fig-%d.png" % fig_n
                    figures.append((name, png))
                    parts.append('<p class="fulltext-equation">'
                                 '<img src="%s%s" loading="lazy" alt="%s"></p>'
                                 % (fig_url_prefix, name,
                                    html.escape(text[:80])))
                    continue

            if is_heading:
                level = toc_titles.get(norm, 1)
                tag = "h3" if level <= 1 else "h4"
                parts.append("<%s>%s</%s>" % (tag, html.escape(text), tag))
            else:
                parts.append("<p>%s</p>" % html.escape(text))

        if started and not in_references:
            for rect in standalone:
                png = _render_clip(page, rect)
                if png:
                    fig_n += 1
                    name = "fig-%d.png" % fig_n
                    figures.append((name, png))
                    parts.append(_figure_html(fig_url_prefix + name, ""))

    return {"html": "".join(parts), "figures": figures}
