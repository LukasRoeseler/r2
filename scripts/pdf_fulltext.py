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

# Footnotes: a footnote's own leading number (e.g. "1" at the start of a
# footnote block at the bottom of the page) is typically NOT
# superscript-flagged in practice (confirmed against this corpus - only
# the in-text reference mark tends to be), so detection relies on font
# size relative to body_size plus page position, not PyMuPDF's
# superscript span flag.
FOOTNOTE_MARKER_RE = re.compile(r"^(\d{1,3})[.\)]?$")
FOOTNOTE_MARKER_SIZE_DELTA = 2.5    # a leading/reference marker span must be
                                    # at least this much smaller than body_size
FOOTNOTE_BODY_DELTA_MIN = 0.5       # a footnote block's own body text is
FOOTNOTE_BODY_DELTA_MAX = 4.0       # this much smaller than body_size
FOOTNOTE_Y_FRAC = 0.55              # ...and starts in the bottom ~45% of the
                                    # page (excludes author/affiliation
                                    # superscripts near the top of page 1,
                                    # which can otherwise look identical)

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


def _assemble_block(block, span_join):
    """Join a text block's lines into one de-hyphenated paragraph string,
    using span_join(spans) to turn each line's own spans into text."""
    text = ""
    for line in block["lines"]:
        piece = span_join(line["spans"]).strip()
        if not piece:
            continue
        if text.endswith("-") and text[-2:-1].isalpha() and piece[:1].islower():
            text = text[:-1] + piece       # re-join a line-break hyphenation
        elif text:
            text += " " + piece
        else:
            text = piece
    return re.sub(r"\s+", " ", text).strip()


def _plain_join(spans):
    """Join one line's spans verbatim - today's behavior, used wherever
    footnote-aware markup isn't needed (furniture/caption matching etc,
    where exact inter-span spacing doesn't affect the comparison)."""
    return "".join(span["text"] for span in spans)


def _spaced_join(spans):
    """Like _plain_join, but inserts a space at a style boundary (size or
    bold changes) when neither side already has whitespace there and both
    boundary characters are alphanumeric. PyMuPDF doesn't reliably carry a
    separating space at such boundaries - e.g. a footnote's small marker
    span immediately followed by its body-text span with no leading
    space, which otherwise glues the marker onto the next word
    ("1The incentive to..."). Punctuation-adjacent and same-style splits
    are left untouched, so real words broken across spans by kerning
    alone aren't affected."""
    out = ""
    prev_style = None
    for span in spans:
        s = span["text"]
        if not s:
            continue
        style = (round(span["size"], 1), bool(span["flags"] & BOLD_FLAG))
        if (out and prev_style is not None and style != prev_style
                and not out[-1].isspace() and not s[:1].isspace()
                and out[-1].isalnum() and s[:1].isalnum()):
            out += " "
        out += s
        prev_style = style
    return out


def _block_text(block):
    """Join a text block's lines into one de-hyphenated paragraph string."""
    return _assemble_block(block, _plain_join)


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


def _leading_marker(block, body_size):
    """If a text block's very first span is a short digit run clearly
    smaller than body text, return the digit string (the footnote's own
    marker) - else None."""
    lines = block.get("lines") or []
    if not lines or not lines[0]["spans"]:
        return None
    first = lines[0]["spans"][0]
    if not first["text"] or first["size"] > body_size - FOOTNOTE_MARKER_SIZE_DELTA:
        return None
    m = FOOTNOTE_MARKER_RE.match(first["text"].strip())
    return m.group(1) if m else None


def _footnote_markers(doc, body_size):
    """{page_index: {marker_digit_str, ...}} for blocks that look like
    footnotes: a small leading marker, non-bold body text sized notably
    below body_size, sitting in the bottom ~45% of the page. Mirrors the
    _body_font_size/_furniture_texts whole-document prepass idiom.
    Footnote identity is page-scoped (not global) since that's how
    footnotes actually work, and it keeps IDs collision-free even if two
    different pages both happen to have a footnote "1"."""
    per_page = {}
    for pno, page in enumerate(doc):
        y_min = page.rect.height * FOOTNOTE_Y_FRAC
        found = set()
        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0 or block["bbox"][1] < y_min:
                continue
            marker = _leading_marker(block, body_size)
            if marker is None:
                continue
            size, bold, _ = _span_stats(block)
            if bold or not (body_size - FOOTNOTE_BODY_DELTA_MAX
                             <= size <= body_size - FOOTNOTE_BODY_DELTA_MIN):
                continue
            found.add(marker)
        if found:
            per_page[pno] = found
    return per_page


FOOTREF_MARKER_RE = re.compile(r"\x00FNREF:(\d+):(\d+)\x00")


def _paragraph_html(block, footnote_ids, page_index, body_size,
                     skip_leading_marker=False):
    """HTML for a paragraph/footnote-body block: the same line/de-hyphenation
    assembly as _block_text(), corrected via _spaced_join, plus two
    footnote-aware behaviors: (1) a small digit span whose value is a
    CONFIRMED footnote number for this page becomes a \\x00FNREF:page:n\\x00
    marker - a footnote's own body text (needed for its in-text reference's
    hover tooltip) isn't known yet at this point in the single forward pass,
    so the final <sup> markup is resolved later, once every footnote on
    every page has been collected (see _resolve_footrefs). NUL/digits/colons
    are untouched by html.escape(), so the marker survives being embedded
    here and escaped normally. Any other small digit (ordinals, unrelated
    superscripts) is left as plain text, unchanged from before footnote
    support existed. (2) if skip_leading_marker, the block's own leading
    marker span is dropped from the output (used when rendering a
    footnote's own body - its number is shown separately via the
    footnote list's <li value=>).
    """
    first_line_spans = block["lines"][0]["spans"] if block["lines"] else None

    def join(spans):
        if skip_leading_marker and spans is first_line_spans:
            nonempty = [s for s in spans if s["text"]]
            if (nonempty and nonempty[0]["size"] <= body_size - FOOTNOTE_MARKER_SIZE_DELTA
                    and FOOTNOTE_MARKER_RE.match(nonempty[0]["text"].strip())):
                spans = spans[spans.index(nonempty[0]) + 1:]
        out = ""
        prev_style = None
        for span in spans:
            s = span["text"]
            if not s:
                continue
            style = (round(span["size"], 1), bool(span["flags"] & BOLD_FLAG))
            stripped = s.strip()
            is_ref = (span["size"] <= body_size - FOOTNOTE_MARKER_SIZE_DELTA
                      and re.match(r"^\d{1,3}$", stripped)
                      and stripped in footnote_ids)
            if (out and prev_style is not None and style != prev_style
                    and not out[-1].isspace() and not s[:1].isspace()
                    and out[-1].isalnum() and s[:1].isalnum()):
                out += " "
            if is_ref:
                out += "\x00FNREF:%d:%s\x00" % (page_index, stripped)
            else:
                out += s
            prev_style = style
        return out

    raw = _assemble_block(block, join)
    return html.escape(raw)


def _resolve_footrefs(html_text, footnote_bodies):
    """Replace every \\x00FNREF:page:n\\x00 marker left by _paragraph_html
    with its final <sup> markup, once every footnote's body text is known
    (footnote_bodies: {(page_index, marker_str): body_html}). Mirrors the
    in-text citation reference's hover-tooltip pattern (.cite-tooltip) so
    hovering a footnote number works the same way as hovering a citation."""
    def repl(m):
        page_index, marker = int(m.group(1)), m.group(2)
        fid = "fn-%d-%s" % (page_index, marker)
        refid = "fnref-%d-%s" % (page_index, marker)
        body = footnote_bodies.get((page_index, marker), "")
        tooltip = ('<span class="cite-tooltip">%s</span>' % body) if body else ""
        return ('<sup class="fulltext-footref"><a href="#%s" id="%s">%s</a>%s</sup>'
                % (fid, refid, html.escape(marker), tooltip))
    return FOOTREF_MARKER_RE.sub(repl, html_text)


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
    footnote_numbers = _footnote_markers(doc, body_size)
    toc = [] if ignore_toc else doc.get_toc()
    toc_titles = {_norm(t[1]): t[0] for t in toc}
    first_heading = _norm(toc[0][1]) if toc else None

    parts = []
    figures = []
    footnotes = []
    started = first_heading is None    # no bookmarks -> include everything
    in_references = False
    fig_n = 0

    for page_index, page in enumerate(doc):
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
            rect = fitz.Rect(block["bbox"])
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

            page_footnote_ids = footnote_numbers.get(page_index, ())
            marker = _leading_marker(block, body_size)
            if (not is_heading and marker is not None and marker in page_footnote_ids
                    and not bold
                    and body_size - FOOTNOTE_BODY_DELTA_MAX <= size <= body_size - FOOTNOTE_BODY_DELTA_MIN
                    and rect.y0 >= page.rect.height * FOOTNOTE_Y_FRAC):
                body_html = _paragraph_html(block, page_footnote_ids, page_index,
                                             body_size, skip_leading_marker=True)
                fid = "fn-%d-%s" % (page_index, marker)
                refid = "fnref-%d-%s" % (page_index, marker)
                footnotes.append((page_index, marker, fid, refid, body_html))
                continue

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
                parts.append("<p>%s</p>" % _paragraph_html(
                    block, page_footnote_ids, page_index, body_size))

        if started and not in_references:
            for rect in standalone:
                png = _render_clip(page, rect)
                if png:
                    fig_n += 1
                    name = "fig-%d.png" % fig_n
                    figures.append((name, png))
                    parts.append(_figure_html(fig_url_prefix + name, ""))

    footnote_bodies = {(page_index, marker): body_html
                        for page_index, marker, fid, refid, body_html in footnotes}
    html_out = _resolve_footrefs("".join(parts), footnote_bodies)

    if footnotes:
        items = "".join(
            '<li id="%s" value="%s">%s '
            '<a class="footnote-backref" href="#%s" aria-label="Back to text">↩</a></li>'
            % (fid, marker, body_html, refid)
            for page_index, marker, fid, refid, body_html in footnotes)
        html_out += ('<section class="fulltext-footnotes" aria-label="Footnotes">'
                     '<ol>%s</ol></section>' % items)

    return {"html": html_out, "figures": figures}
