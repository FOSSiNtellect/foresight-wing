#!/usr/bin/env python3
"""The Foresight Wing press: prints the README as a newspaper.

A special edition from the FOSSiNtellect presses (github.com/FOSSiNtellect), set in the same type. It reads
edition.md (the articles) and edition.toml (the front page, newsreel, diagrams and classifieds), prints every page
as docs/readme/*.svg plus a night edition for GitHub's dark theme in docs/readme/dark/, and writes README.md: the
pages in order, then the same words as a plain-text edition. Standard library only, so it runs anywhere Python 3.11+
does. The moving newsreel is printed separately from the demo video by newsreel.py.

    python docs/press/press.py
"""
from __future__ import annotations

import base64
import html
import json
import math
import random
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[2]
PRESS = ROOT / "docs" / "press"
FONTS = PRESS / "fonts"
OUT = ROOT / "docs" / "readme"
README = ROOT / "README.md"

W = 840  # every page is drawn on an 840-unit canvas, the width of GitHub's README column
# The day edition is printed in black and white; only the margin notes are in pen.
# Every colour here must be distinct, since the night edition swaps them one for one.
INK = "#141414"
INK_SOFT = "#595959"
ACCENT = "#262626"
PEN = "#23428c"  # the blue ballpoint of the margin notes
PAPER_TONES = ("#ffffff", "#fefefe", "#fdfdfd", "#fcfcfc")

# The night edition, for GitHub's dark theme: every day colour above maps to one here.
NIGHT = {INK: "#e9e4d8", INK_SOFT: "#a29b8d", ACCENT: "#e8826f", PEN: "#8cb2ff",
         **dict(zip(PAPER_TONES, ("#1c1f25", "#1e2128", "#1a1d23", "#20232a")))}
NIGHT_PHOTOS: dict[str, str] = {}  # a photo's day print (base64) → its night print, filled in by Page.photo


# ── Type ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Face:
    key: str
    css: str
    upm: int
    cap: float
    widths: dict[str, int]

    def width(self, text: str, size: float, tracking: float = 0.0) -> float:
        fallback = self.widths.get("n", self.upm // 2)
        units = sum(self.widths.get(ch, fallback) for ch in text)
        return units / self.upm * size + tracking * len(text)


def _load_faces() -> dict[str, Face]:
    metrics = json.loads((FONTS / "metrics.json").read_text(encoding="utf-8"))
    css = {"serif": "r", "serif-bold": "b", "serif-italic": "i", "hand": "h"}
    return {k: Face(k, css[k], m["upm"], m["capHeight"] / m["upm"], m["widths"])
            for k, m in metrics.items()}


FACES = _load_faces()
R, B, I, H = (FACES[k] for k in ("serif", "serif-bold", "serif-italic", "hand"))
# Code is set in the reader's own monospace (Consolas, Menlo…), a size smaller than the text around it.
MONO_SCALE = 0.86
CHAR = 0.55  # a monospace character is about 0.55 em wide
M = Face("mono", "m", 1000, 0.66, {"n": round(CHAR * 1000 * MONO_SCALE)})

FONT_RULES = {
    "serif": ("OldStandard", "normal", "normal"),
    "serif-bold": ("OldStandard", "700", "normal"),
    "serif-italic": ("OldStandard", "normal", "italic"),
    "hand": ("Caveat", "normal", "normal"),
}
BASE_CSS = (
    'text{fill:%s;font-family:OldStandard,Georgia,"Times New Roman",serif;'
    "font-kerning:none;font-variant-ligatures:none}"
    ".r{font-family:OldStandard,Georgia,serif}"
    ".b{font-family:OldStandard,Georgia,serif;font-weight:700}"
    ".i{font-family:OldStandard,Georgia,serif;font-style:italic}"
    '.h{font-family:Caveat,"Segoe Print","Bradley Hand",cursive;font-kerning:normal}'
    '.m{font-family:Consolas,"Cascadia Mono",Menlo,"Courier New",monospace;white-space:pre}' % INK
)


def paper_defs(grain: tuple, stain: tuple, edge: str, edge_opacity: float, shadow: float) -> str:
    """Shadow, grain, faint stains and darker edges for a sheet. grain/stain are RGB in 0-1."""
    (gr, gg, gb), (sr, sg, sb) = grain, stain
    return (
        '<filter id="shadow" x="-4%" y="-4%" width="108%" height="108%">'
        f'<feDropShadow dx="0" dy="1.6" stdDeviation="2.6" flood-color="#000" flood-opacity="{shadow:g}"/></filter>'
        '<filter id="grain" x="0" y="0" width="100%" height="100%">'
        '<feTurbulence type="fractalNoise" baseFrequency=".9" numOctaves="2" seed="3" stitchTiles="stitch"/>'
        f'<feColorMatrix values="0 0 0 0 {gr:g} 0 0 0 0 {gg:g} 0 0 0 0 {gb:g} 1.3 0 0 0 -.52"/></filter>'
        '<filter id="stain" x="0" y="0" width="100%" height="100%">'
        '<feTurbulence type="fractalNoise" baseFrequency=".005 .008" numOctaves="2" seed="11"/>'
        f'<feColorMatrix values="0 0 0 0 {sr:g} 0 0 0 0 {sg:g} 0 0 0 0 {sb:g} 1.7 0 0 0 -.8"/></filter>'
        '<radialGradient id="age" cx="50%" cy="45%" r="75%">'
        f'<stop offset=".62" stop-color="{edge}" stop-opacity="0"/>'
        f'<stop offset="1" stop-color="{edge}" stop-opacity="{edge_opacity:g}"/></radialGradient>'
    )


DEFS = paper_defs((.55, .55, .55), (1, 1, 1), "#000000", 0, 0.18)  # clean white: faint grain, no stains
NIGHT_DEFS = paper_defs((.36, .37, .4), (0, 0, 0), "#000000", 0.3, 0.55)


def night_edition(svg: str) -> str:
    """The same page for GitHub's dark theme: dark newsprint, light ink."""
    svg = svg.replace(DEFS, NIGHT_DEFS)
    for day, night in (*NIGHT.items(), *NIGHT_PHOTOS.items()):
        svg = svg.replace(day, night)
    return svg


Word = tuple[str, Face]


def words_of(text: str, face: Face) -> list[Word]:
    return [(w, face) for w in text.split()]


INLINE = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`|\*(.+?)\*|\[([^\]]+)\]\([^)]+\)")


def _pieces(text: str, base: Face) -> list[tuple[str, Face]]:
    """Runs of text with their faces; bold and italic runs may hold `code` or links of their own."""
    out, pos = [], 0
    for m in INLINE.finditer(text):
        out.append((text[pos:m.start()], base))
        bold, code, italic, link = m.groups()
        if bold is not None:
            out += _pieces(bold, B)
        elif code is not None:
            out.append((code, M))
        elif italic is not None:
            out += _pieces(italic, I if base is not I else R)
        else:
            out += _pieces(link, base)
        pos = m.end()
    out.append((text[pos:], base))
    return out


def rich(text: str, base: Face = R) -> list[Word]:
    """Words of a line of Markdown: **bold**, *italic*, `code` and [links] keep their face (links print as text)."""
    words: list[Word] = []
    glue = False  # whether the next piece continues the last word (no space between them)

    def add(piece: str, face: Face):
        nonlocal glue
        for k, part in enumerate(piece.split(" ")):
            if k:
                glue = False
            if not part:
                continue
            if glue and words:
                prev, prev_face = words[-1]
                # punctuation joins the word it touches, in that word's face
                face_of = face if not any(ch.isalnum() for ch in prev) else prev_face
                words[-1] = (prev + part, face_of)
            else:
                words.append((part, face))
            glue = True

    for piece, face in _pieces(text, base):
        add(piece, face)
    return words


def plain(text: str) -> str:
    """The words of a line of Markdown without its markup."""
    return " ".join(w for w, _ in rich(text))


VOWELS = set("aeiouy")
UNSPLIT = {"th", "ch", "sh", "ph", "wh", "gh", "ck"}


def hyphen_points(word: str) -> list[int]:
    """Rough English syllable breaks (V-CV and VC-CV), longest prefix first."""
    if "-" in word:
        return [i + 1 for i, ch in enumerate(word[:-3]) if ch == "-" and i >= 2][::-1]
    core = word.rstrip(".,;:!?’”)")
    if len(core) < 7 or not core.isalpha() or not core.islower():  # never split names
        return []
    low = core.lower()
    points = []
    for i in range(3, len(core) - 3):
        a, b, c = low[i - 1], low[i], low[i + 1]
        if a in VOWELS and b not in VOWELS and c in VOWELS:
            points.append(i)
        elif a not in VOWELS and b not in VOWELS and low[i - 2] in VOWELS and c in VOWELS and a + b not in UNSPLIT:
            points.append(i)
    return points[::-1]


def break_lines(words: list[Word], size: float, width_for, hyphenate=False) -> list[list[Word]]:
    """Greedy line breaking; width_for(n) is the measure available to line n.

    With hyphenate, a word that would leave a loose line is split across it (never code).
    """
    space = R.width(" ", size)
    lines: list[list[Word]] = []
    line: list[Word] = []
    used = 0.0
    queue = list(words)
    while queue:
        word = queue.pop(0)
        ww = word[1].width(word[0], size)
        limit = width_for(len(lines))
        if line and used + space + ww > limit:
            if hyphenate and word[1] is not M and limit - used > space * 1.6:
                text, face = word
                for i in hyphen_points(text):
                    head = text[:i] if text[i - 1] == "-" else text[:i] + "-"
                    if used + space + face.width(head, size) <= limit:
                        line.append((head, face))
                        queue.insert(0, (text[i:], face))
                        break
                else:
                    queue.insert(0, word)
            else:
                queue.insert(0, word)
            lines.append(line)
            line, used = [], 0.0
        else:
            used = ww if not line else used + space + ww
            line.append(word)
    if line:
        lines.append(line)
    return lines


def balanced(words: list[Word], size: float, width: float) -> list[list[Word]]:
    """Breaks into the fewest lines that fit, then evens out their lengths."""
    target = len(break_lines(words, size, lambda _: width))
    lo, hi = width * 0.4, width
    for _ in range(20):
        mid = (lo + hi) / 2
        if len(break_lines(words, size, lambda _: mid)) <= target:
            hi = mid
        else:
            lo = mid
    return break_lines(words, size, lambda _: hi)


def clamp_lines(lines: list[list[Word]], size: float, width: float, limit: int) -> list[list[Word]]:
    """Keeps at most `limit` lines, ending the last with an ellipsis if text was cut."""
    if len(lines) <= limit:
        return lines
    kept = [list(line) for line in lines[:limit]]
    last = kept[-1]
    while last:
        text, face = last[-1]
        last[-1] = (text.rstrip(",;:.-") + "…", face)
        if line_width(last, size) <= width:
            break
        last[-1] = (text, face)
        last.pop()
    return kept


def line_width(line: list[Word], size: float) -> float:
    return sum(f.width(w, size) for w, f in line) + R.width(" ", size) * (len(line) - 1)


def fit_size(text: str, face: Face, width: float, largest: float, smallest: float, tracking=0.0) -> float:
    size = largest
    while size > smallest and face.width(text, size, tracking) > width:
        size -= 0.5
    return size


def set_story(paragraphs: list[str], body: float, colw: float):
    """Breaks the lead story into column lines, with a three-line drop cap.

    Returns (leading, drop cap size, [(words, x offset, ends paragraph)]).
    """
    lead_h = round(body * 1.36, 1)
    cap_size = round((2 * lead_h + body * R.cap) / B.cap, 1)
    cap_w = B.width(paragraphs[0][0], cap_size) + 6
    lines = []
    for p, para in enumerate(paragraphs):
        def width_for(n, p=p):
            if p == 0 and n < 3:
                return colw - cap_w
            return colw - (16 if p > 0 and n == 0 else 0)
        text = para[1:].lstrip() if p == 0 else para
        broken = break_lines(words_of(text, R), body, width_for, hyphenate=True)
        lines += [(ln, colw - width_for(n), n == len(broken) - 1) for n, ln in enumerate(broken)]
    return lead_h, cap_size, lines


# ── Pages ───────────────────────────────────────────────────────────────

class Page:
    """An SVG page under construction. Fonts are embedded only if used."""

    def __init__(self, width: float = W):
        self.width = width
        self.under: list[str] = []
        self.parts: list[str] = []
        self.used: set[str] = set()

    def add(self, markup: str):
        self.parts.append(markup)

    def text(self, x, y, s, face: Face, size, *, anchor="start", fill=INK, tracking=0.0):
        self.used.add(face.key)
        if anchor == "middle" and tracking:
            x += tracking / 2  # letter-spacing also trails the last letter
        attrs = f' text-anchor="{anchor}"' if anchor != "start" else ""
        attrs += f' letter-spacing="{tracking:g}"' if tracking else ""
        attrs += f' fill="{fill}"' if fill != INK else ""
        self.add(f'<text x="{x:.1f}" y="{y:.1f}" class="{face.css}" font-size="{size:g}"{attrs}>{escape(s)}</text>')

    def line(self, line: list[Word], x, y, size, *, justify_to=None, anchor="start", fill=INK):
        """Sets one line of words; with justify_to, spreads it to that measure."""
        if not line:
            return
        self.used.update(f.key for _, f in line)
        space = R.width(" ", size)
        widths = [f.width(w, size) for w, f in line]
        gap = space
        if justify_to and len(line) > 1:
            gap = (justify_to - sum(widths)) / (len(line) - 1)
            if gap > space * 3:  # too loose to justify; leave it ragged
                gap = space
        total = sum(widths) + gap * (len(line) - 1)
        x -= {"start": 0, "middle": total / 2, "end": total}[anchor]
        spans = []
        for (w, f), ww in zip(line, widths):
            smaller = f' font-size="{size * MONO_SCALE:.2f}"' if f is M else ""
            spans.append(f'<tspan x="{x:.1f}" class="{f.css}"{smaller}>{escape(w)}</tspan>')
            x += ww + gap
        color = f' fill="{fill}"' if fill != INK else ""
        self.add(f'<text y="{y:.1f}" font-size="{size:g}"{color}>{"".join(spans)}</text>')

    def code(self, x, y, text, size):
        """One line of code, spaces kept."""
        self.add(f'<text x="{x:.1f}" y="{y:.1f}" class="m" font-size="{size:g}" xml:space="preserve">{escape(text)}</text>')

    def rule(self, x1, y, x2, weight=0.8, dash=None, color=INK):
        extra = f' stroke-dasharray="{dash}" stroke-linecap="round"' if dash else ""
        self.add(f'<line x1="{x1:.1f}" y1="{y:.1f}" x2="{x2:.1f}" y2="{y:.1f}" stroke="{color}" stroke-width="{weight:g}"{extra}/>')

    def vrule(self, x, y1, y2, weight=0.6):
        self.add(f'<line x1="{x:.1f}" y1="{y1:.1f}" x2="{x:.1f}" y2="{y2:.1f}" stroke="{INK}" stroke-width="{weight:g}"/>')

    def box(self, x, y, w, h, weight=0.8, inset=0.0, dash=None):
        extra = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" fill="none" stroke="{INK}" stroke-width="{weight:g}"{extra}/>')
        if inset:
            self.add(f'<rect x="{x + inset:.1f}" y="{y + inset:.1f}" width="{w - 2 * inset:.1f}" height="{h - 2 * inset:.1f}" fill="none" stroke="{INK}" stroke-width=".5"/>')

    def diamond(self, cx, cy, r, fill=INK):
        self.add(f'<polygon points="{cx:.1f},{cy - r:.1f} {cx + r:.1f},{cy:.1f} {cx:.1f},{cy + r:.1f} {cx - r:.1f},{cy:.1f}" fill="{fill}"/>')

    def pointer(self, x1, y1, x2, y2, weight=0.9, head=5.5):
        """A printed arrow: a straight ink line ending in a solid head at (x2, y2)."""
        ang = math.atan2(y2 - y1, x2 - x1)
        bx, by = x2 - head * math.cos(ang), y2 - head * math.sin(ang)
        self.add(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{bx:.1f}" y2="{by:.1f}" stroke="{INK}" stroke-width="{weight:g}"/>')
        pts = [(x2, y2)] + [(x2 - head * math.cos(ang + d) * 1.15, y2 - head * math.sin(ang + d) * 1.15) for d in (0.42, -0.42)]
        self.add(f'<polygon points="{" ".join(f"{px:.1f},{py:.1f}" for px, py in pts)}" fill="{INK}"/>')

    def photo(self, x, y, w, h, jpeg: Path, night: Path | None = None):
        """A halftone print. The night edition swaps in `night`, so a dark screenshot can print light by day."""
        data = base64.b64encode(jpeg.read_bytes()).decode()
        if night:
            NIGHT_PHOTOS[data] = base64.b64encode(night.read_bytes()).decode()
        self.add(f'<image x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" preserveAspectRatio="xMidYMid slice" '
                 f'href="data:image/jpeg;base64,{data}"/>')

    def note(self, x, y, text, size=23, tilt=-3.0, anchor="start") -> float:
        """A handwritten margin note in blue pen. Returns its width."""
        self.used.add("hand")
        a = f' text-anchor="{anchor}"' if anchor != "start" else ""
        self.add(f'<text x="{x:.1f}" y="{y:.1f}" class="h" font-size="{size:g}" fill="{PEN}"{a} '
                 f'transform="rotate({tilt:g} {x:.1f} {y:.1f})">{escape(text)}</text>')
        return H.width(text, size)

    def arrow(self, x1, y1, x2, y2, bend=0.3):
        """A hand-drawn pen arrow. Positive bend bows it to the left of its travel."""
        dx, dy = x2 - x1, y2 - y1
        cx, cy = (x1 + x2) / 2 + dy * bend, (y1 + y2) / 2 - dx * bend
        ang = math.atan2(y2 - cy, x2 - cx)
        head = " ".join(f"M{x2:.1f} {y2:.1f} L{x2 - 9 * math.cos(ang + d):.1f} {y2 - 9 * math.sin(ang + d):.1f}"
                        for d in (0.5, -0.42))
        self.add(f'<path d="M{x1:.1f} {y1:.1f} Q{cx:.1f} {cy:.1f} {x2:.1f} {y2:.1f} {head}" fill="none" '
                 f'stroke="{PEN}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>')

    def sheet(self, x, y, w, h, *, seed: int, tone=PAPER_TONES[0], ragged=1.1):
        """A piece of newsprint, cut a little unevenly, with grain and age."""
        rng = random.Random(seed)
        corners = [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]
        path = "M" + " L".join(f"{cx + rng.uniform(-ragged, ragged):.1f} {cy + rng.uniform(-ragged, ragged):.1f}"
                               for cx, cy in corners) + " Z"
        cid = f"cut{seed}"
        area = f'x="{x - 4:.1f}" y="{y - 4:.1f}" width="{w + 8:.1f}" height="{h + 8:.1f}"'
        self.under.append(
            f'<clipPath id="{cid}"><path d="{path}"/></clipPath>'
            f'<path d="{path}" fill="{tone}" filter="url(#shadow)"/>'
            f'<g clip-path="url(#{cid})"><rect {area} filter="url(#stain)" opacity=".15"/>'
            f'<rect {area} filter="url(#grain)" opacity=".3"/><rect {area} fill="url(#age)"/></g>'
        )

    def render(self, height: float, *, title: str, rotate: float = 0.0) -> str:
        fonts = []
        for key in sorted(self.used & FONT_RULES.keys()):  # code uses the reader's own monospace
            family, weight, style = FONT_RULES[key]
            data = base64.b64encode((FONTS / f"{key}.woff2").read_bytes()).decode()
            fonts.append(f'@font-face{{font-family:{family};font-weight:{weight};font-style:{style};'
                         f'src:url(data:font/woff2;base64,{data}) format("woff2")}}')
        body = "".join(self.under) + "".join(self.parts)
        if rotate:
            body = f'<g transform="rotate({rotate:g} {self.width / 2:.1f} {height / 2:.1f})">{body}</g>'
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.width:g} {height:.0f}" '
                f'width="{self.width:g}" height="{height:.0f}" role="img">'
                f'<title>{escape(title)}</title><defs>{DEFS}</defs>'
                f'<style>{"".join(fonts)}{BASE_CSS}</style>{body}</svg>')


def kicker(page: Page, x, y, text, *, anchor="middle"):
    """A small tracked headline label between two short rules."""
    width = B.width(text, 10.5, 3.5)
    page.text(x, y, text, B, 10.5, anchor=anchor, fill=ACCENT, tracking=3.5)
    if anchor == "middle":
        for side in (-1, 1):
            page.rule(x + side * (width / 2 + 10), y - 3.6, x + side * (width / 2 + 40), 0.8, color=ACCENT)


def headline(page: Page, mid, y, text, width, largest=34.0, smallest=22.0) -> float:
    """A centred headline on one line (or two, balanced). Returns the baseline of its last line."""
    size = fit_size(text, B, width, largest, smallest)
    lines = [words_of(text, B)] if B.width(text, size) <= width else balanced(words_of(text, B), smallest, width)
    size = size if len(lines) == 1 else smallest
    for n, ln in enumerate(lines):
        page.line(ln, mid, y + size * 0.72 + n * size * 1.08, size, anchor="middle")
    return y + size * 0.72 + (len(lines) - 1) * size * 1.08


def centred_deck(page: Page, mid, y, text, width, size=14.5) -> float:
    lines = balanced(words_of(text, I), size, width)
    for n, ln in enumerate(lines):
        page.line(ln, mid, y + n * size * 1.3, size, anchor="middle")
    return y + (len(lines) - 1) * size * 1.3


# ── Boxed items (the architecture pages) ────────────────────────────────

BODY = 11.4
LEAD = round(BODY * 1.34, 1)


def _card_lines(body: str, inner: float):
    return break_lines(words_of(body, R), BODY, lambda _: inner, hyphenate=True)


def card_height(w: float, title: str, files: str, body: str, pad=10) -> float:
    lines = _card_lines(body, w - 2 * pad)
    return pad + 13 + (15 if files else 0) + 9 + (len(lines) - 1) * LEAD + pad + 4


def card(page: Page, x, y, w, h, title: str, files: str, body: str, *, label: str = "", pad=10):
    """A ruled box: a bold title (after an optional step label), file names in italics, then the text."""
    inner = w - 2 * pad
    page.box(x, y, w, h, 0.9)
    ty = y + pad + 11
    if label:
        lw = B.width(label, 9, 1.6)
        page.text(x + pad, ty, label, B, 9, fill=ACCENT, tracking=1.6)
        page.text(x + pad + lw + 6, ty, title, B, fit_size(title, B, inner - lw - 6, 12, 9))
    else:
        page.text(x + pad, ty, title, B, fit_size(title, B, inner, 13, 9.5))
    if files:
        ty += 15
        page.text(x + pad, ty, files, I, fit_size(files, I, inner, 10.5, 8), fill=INK_SOFT)
    ty += 6
    page.rule(x + pad, ty, x + pad + 26, 1.2)
    ty += 16
    for k, ln in enumerate(_card_lines(body, inner)):
        page.line(ln, x + pad, ty + k * LEAD, BODY)


def card_row(page: Page, y, x0, width, gap, items: list[dict], *, labels=None) -> float:
    """Lays items out side by side at equal width and height. Returns the row's height."""
    w = (width - gap * (len(items) - 1)) / len(items)
    h = max(card_height(w, it["title"], it.get("files", ""), it["body"]) for it in items)
    for n, it in enumerate(items):
        card(page, x0 + n * (w + gap), y, w, h, it["title"], it.get("files", ""), it["body"],
             label=labels[n] if labels else "")
    return h


def band_label(page: Page, x, y, text, note=""):
    page.text(x, y, text, B, 10, tracking=2.4)
    if note:
        page.text(x + B.width(text, 10, 2.4) + 12, y, note, I, 11.5, fill=INK_SOFT)


# ── The front page ──────────────────────────────────────────────────────

def front_page(ed: dict) -> str:
    paper, lead, notes = ed["paper"], ed["lead"], ed.get("notes", {})
    page = Page()
    top, x0, x1 = 10, 40, W - 40
    cw, mid = x1 - x0, W / 2
    y = top + 26

    # Flag: the publisher, nameplate and motto.
    flag_w = B.width(paper["flag"], 10, 3.5)
    page.text(mid, y + 8, paper["flag"], B, 10, anchor="middle", tracking=3.5)
    for side in (-1, 1):
        inner = mid + side * (flag_w / 2 + 12)
        page.diamond(inner, y + 4.5, 2.6)
        page.rule(inner + side * 7, y + 4.5, inner + side * 34, 0.7)
    y += 24
    size = round(min(72.0, cw * 0.86 / B.width(paper["title"], 1)), 1)
    title_w = B.width(paper["title"], size)
    page.text(mid, y + size * 0.76, paper["title"], B, size, anchor="middle")
    if notes.get("masthead"):
        nw = page.note(x0 - 14, y - 2, notes["masthead"], 21, tilt=-4)
        page.arrow(x0 - 14 + nw * 0.55, y + 6, mid - title_w / 2 + 6, y + size * 0.3, bend=0.35)
    y += size * 0.9
    page.text(mid, y + 14, paper["motto"], I, 14, anchor="middle")
    y += 28

    # Folio line.
    folio = y
    page.rule(x0, y, x1, 0.9)
    page.text(x0, y + 16.5, paper["volume"], B, 9.5, tracking=1.6)
    page.text(mid, y + 16.5, paper["date"], B, 10.5, anchor="middle", tracking=2.2)
    page.text(x1, y + 16.5, paper["price"], B, 9.5, anchor="end", tracking=1.6)
    page.rule(x0, y + 24, x1, 0.9)
    page.rule(x0, y + 27.8, x1, 2.6)
    y += 58

    # Kicker, with the date note in the margin beside it.
    kicker(page, mid, y, lead["kicker"])
    if notes.get("date"):
        nw = page.note(x0 + 6, y + 4, notes["date"], 22, tilt=-3)
        page.arrow(x0 + 6 + nw + 6, y - 12, mid - B.width(paper["date"], 10.5, 2.2) / 2 - 10, folio + 13, bend=0.45)

    # Headline and deck.
    y += 16
    y = headline(page, mid, y, lead["headline"], cw, 50, 36) + 14
    deck = balanced(words_of(lead["deck"], I), 17, cw * 0.84)
    for n, ln in enumerate(deck):
        page.line(ln, mid, y + 13 + n * 22, 17, anchor="middle")
    y += 13 + (len(deck) - 1) * 22 + 30

    # Story on the left, the staff on the right.
    gutter = 30
    sw = 450
    tw = cw - sw - gutter
    rx = x0 + sw + gutter
    by_w = B.width(lead["byline"], 10, 2.2)
    page.text(x0 + sw / 2, y, lead["byline"], B, 10, anchor="middle", tracking=2.2)
    for side in (-1, 1):
        page.rule(x0 + sw / 2 + side * (by_w / 2 + 12), y - 3.6, x0 + sw / 2 + side * sw / 2, 0.6)
    staff = ed["staff"]
    page.text(rx + tw / 2, y, staff["heading"], B, 10, anchor="middle", tracking=2.2)
    page.rule(rx, y + 9, rx + tw, 0.8)
    name_w = max(B.width(m["name"], 14, 0) for m in staff["members"]) + 14
    ty = y + 32
    for m in staff["members"]:
        page.text(rx, ty, m["name"], B, 14)
        page.text(rx + name_w, ty, m["beat"], I, fit_size(m["beat"], I, tw - name_w - 14, 13.5, 10), fill=INK_SOFT)
        page.diamond(rx + tw - 4, ty - 4.5, 3.2)
        page.rule(rx, ty + 8.5, rx + tw, 0.6, dash="0.6 2.6")
        ty += 25
    col_bottom = ty - 16
    if notes.get("staff"):
        page.note(rx + 96, col_bottom + 36, notes["staff"], 23, tilt=-3)
        page.arrow(rx + 88, col_bottom + 26, rx + 58, col_bottom + 6, bend=-0.35)
        col_bottom += 44

    # The story's type size is picked so it runs about as deep as the staff column.
    story_top = y + 28
    for body in (14.5, 15.0, 15.5, 16.0, 16.5, 17.0):
        lead_h, cap_size, lines = set_story(lead["story"], body, sw)
        story_bottom = story_top + (len(lines) - 1) * lead_h + 6
        if story_bottom >= col_bottom - lead_h:
            break
    page.text(x0 - 1, story_top + 2 * lead_h, lead["story"][0][0], B, cap_size)
    for n, (ln, offset, last) in enumerate(lines):
        page.line(ln, x0 + offset, story_top + n * lead_h, body, justify_to=None if last else sw - offset)
    y = max(story_bottom, col_bottom) + 14
    page.vrule(x0 + sw + gutter / 2, story_top - 30, y - 6)

    # One line of figures.
    figures = "   ·   " + "   ·   ".join(ed["figures"]["items"])
    fig_size = fit_size("THE FIGURES" + figures, R, cw - 40, 11.5, 9, 0.4)
    page.rule(x0, y, x1, 2.4)
    page.rule(x0, y + 4, x1, 0.8)
    page.used.update(("serif", "serif-bold"))
    page.add(f'<text x="{mid}" y="{y + 22:.1f}" text-anchor="middle" font-size="{fig_size:g}">'
             f'<tspan class="b" letter-spacing="2.2">THE FIGURES</tspan>'
             f'<tspan class="r">{escape(figures)}</tspan></text>')
    page.rule(x0, y + 32, x1, 0.8)
    page.rule(x0, y + 35.8, x1, 2.6)
    y += 37 + 24

    page.sheet(12, top, W - 24, y - top, seed=1)
    return page.render(y + 12, title=f"{paper['title']}: {lead['headline']}")


# ── Special inside pages ────────────────────────────────────────────────

def section_banner(title: str, subtitle: str, seed: int) -> str:
    page = Page()
    tw = B.width(title, 15, 4.5)
    page.text(W / 2, 37, title, B, 15, anchor="middle", tracking=4.5)
    for side in (-1, 1):
        inner = W / 2 + side * (tw / 2 + 14)
        page.diamond(inner, 32, 3)
        page.rule(inner + side * 8, 32, inner + side * 130, 0.8)
    page.text(W / 2, 55, subtitle, I, fit_size(subtitle, I, W - 120, 12, 9.5), anchor="middle")
    page.sheet(12, 8, W - 24, 60, seed=seed, tone=PAPER_TONES[0])
    return page.render(76, title=title)


NEWSREEL_PHOTO: tuple[float, float, float, float] = (0, 0, 0, 0)  # where newsreel.py plays the demo: x, y, w, h


def newsreel(ed: dict) -> str:
    """The newsreel clipping, with a still in its photo. newsreel.py prints the same page with the demo playing."""
    global NEWSREEL_PHOTO
    reel, notes = ed["newsreel"], ed.get("notes", {})
    page = Page()
    x0, x1 = 40, W - 40
    cw, mid = x1 - x0, W / 2
    page.text(x0, 46, reel["kicker"], B, 10, fill=ACCENT, tracking=2.4)
    page.text(x1, 46, reel["label"], B, 9.5, anchor="end", tracking=1.6)
    page.rule(x0, 55, x1, 0.9)
    y = round(headline(page, mid, 68, reel["headline"], cw, 30, 22) + 18)

    ph = round(cw * 9 / 16)
    page.photo(x0, y, cw, ph, PRESS / "newsreel.jpg", night=PRESS / "newsreel-night.jpg")
    page.box(x0, y, cw, ph, 1.4)
    page.box(x0 - 4, y - 4, cw + 8, ph + 8, 0.6)
    NEWSREEL_PHOTO = (x0, y, cw, ph)
    y += ph + 12
    page.text(x1, y + 6, f"PHOTO: {reel['credit']}", B, 8.5, anchor="end", fill=INK_SOFT, tracking=1.2)
    if notes.get("newsreel"):
        nw = page.note(x0 + 2, y + 18, notes["newsreel"], 22, tilt=-2)
        page.arrow(x0 + nw + 10, y + 6, x0 + nw + 50, y - 5, bend=0.35)  # stops under the frame: the film plays inside
    y += 44
    lines = break_lines(words_of(reel["caption"], I), 13.5, lambda _: cw, hyphenate=True)
    for k, ln in enumerate(lines):
        page.line(ln, x0, y + k * 18.5, 13.5, justify_to=None if k == len(lines) - 1 else cw)
    y += (len(lines) - 1) * 18.5 + 18
    page.rule(x0, y, x1, 0.6)
    page.text(x0, y + 18, reel["footer"], I, 12, fill=INK_SOFT)
    page.text(x1, y + 18, reel["action"], B, 10, anchor="end", tracking=1.6)
    y += 34
    page.sheet(16, 14, W - 32, y - 14 + 8, seed=30, tone=PAPER_TONES[1])
    return page.render(y + 22, title=reel["headline"])


def architecture(ed: dict) -> str:
    arch, notes = ed["architecture"], ed.get("notes", {})
    page = Page()
    x0, x1 = 40, W - 40
    cw, mid = x1 - x0, W / 2
    kicker(page, mid, 46, arch["kicker"])
    y = headline(page, mid, 58, arch["headline"], cw, 32, 24) + 22
    y = centred_deck(page, mid, y, arch["deck"], cw * 0.8) + 26

    # What comes in.
    band_label(page, x0, y, "WHAT COMES IN")
    y += 10
    h = card_row(page, y, x0, cw, 14, arch["inputs"])
    w_in = (cw - 14 * 3) / 4
    y += h
    frame_top = y + 30
    for n in range(4):
        cx = x0 + n * (w_in + 14) + w_in / 2
        page.pointer(cx, y + 3, cx, frame_top - 2)

    # This PC: the window, the app and the models, inside one dashed frame.
    pad = 16
    fx0, fx1 = x0, x1
    ix0, iw = fx0 + pad, (fx1 - fx0) - 2 * pad
    y = frame_top + 28
    page.text(fx0 + pad, frame_top + 18, arch["frame"], B, 11, tracking=3)
    win = arch["window"]
    band_label(page, ix0, y + 12, win["label"])
    y += 22
    h = card_row(page, y, ix0, iw, 0, [win])
    y += h
    page.pointer(mid - 30, y + 3, mid - 30, y + 23)
    page.pointer(mid + 30, y + 23, mid + 30, y + 3)
    page.text(mid + 40, y + 17, "events, answers", I, 10.5, fill=INK_SOFT)
    page.text(mid - 40, y + 17, "clicks, questions", I, 10.5, anchor="end", fill=INK_SOFT)
    y += 40
    band_label(page, ix0, y, arch["app_label"], arch["app_note"])
    y += 10
    desks = arch["desks"]
    for row in (desks[:3], desks[3:]):
        h = card_row(page, y, ix0, iw, 12, row)
        y += h + 12
    page.pointer(mid, y - 9, mid, y + 13)
    y += 30
    band_label(page, ix0, y, arch["models_label"])
    y += 10
    h = card_row(page, y, ix0, iw, 12, arch["models"])
    y += h + pad
    page.box(fx0, frame_top, fx1 - fx0, y - frame_top, 1.2, dash="5 3")
    if notes.get("frame"):
        nx = fx0 + pad + B.width(arch["frame"], 11, 3) + 28
        nw = page.note(nx, frame_top + 22, notes["frame"], 21, tilt=-2)
        page.arrow(nx + nw + 8, frame_top + 13, nx + nw + 62, frame_top + 2, bend=0.3)

    # The only doors out.
    y += 26
    page.text(x0, y, arch["doors_title"], B, 10, tracking=2.4)
    dw = B.width(arch["doors_title"], 10, 2.4) + 12
    lines = break_lines(words_of(arch["doors"], I), 12.5, lambda n: cw - (dw if n == 0 else 0))
    for k, ln in enumerate(lines):
        page.line(ln, x0 + (dw if k == 0 else 0), y + k * 17, 12.5)
    y += (len(lines) - 1) * 17 + 22
    page.sheet(12, 12, W - 24, y - 12, seed=50, tone=PAPER_TONES[0])
    return page.render(y + 14, title=arch["headline"])


def pipelines(ed: dict) -> str:
    pipes, notes = ed["pipelines"], ed.get("notes", {})
    page = Page()
    x0, x1 = 40, W - 40
    cw, mid = x1 - x0, W / 2
    kicker(page, mid, 46, pipes["kicker"])
    y = headline(page, mid, 58, pipes["headline"], cw, 30, 22) + 30
    gap = 22
    for strip in pipes["lines"]:
        page.text(x0, y, strip["title"], B, 10.5, tracking=2.4)
        page.rule(x0 + B.width(strip["title"], 10.5, 2.4) + 10, y - 3.6, x1, 0.6)
        y += 12
        steps = [{"title": title, "body": body} for title, body in strip["steps"]]
        labels = [f"{n}" for n in range(1, len(steps) + 1)]
        h = card_row(page, y, x0, cw, gap, steps, labels=labels)
        w = (cw - gap * (len(steps) - 1)) / len(steps)
        for n in range(len(steps) - 1):
            ax = x0 + (n + 1) * w + n * gap
            page.pointer(ax + 4, y + h / 2, ax + gap - 3, y + h / 2)
        if notes.get("rerank") and any(t == "RE-RANK" for t, _ in strip["steps"]):
            k = [t for t, _ in strip["steps"]].index("RE-RANK")
            cx = x0 + k * (w + gap) + w / 2
            page.note(cx - 70, y + h + 30, notes["rerank"], 20, tilt=-2)
            page.arrow(cx - 76, y + h + 22, cx - 20, y + h + 3, bend=0.3)
            y += 26
        y += h + 34
    y -= 8
    page.sheet(12, 12, W - 24, y - 12, seed=51, tone=PAPER_TONES[2])
    return page.render(y + 14, title=pipes["headline"])


@dataclass
class Ad:
    slug: str
    head: str
    sub: str
    body: str
    contact: str
    link: str
    plain: str  # how the plain-text edition names it


def ad_clipping(ad: Ad, n: int) -> str:
    cw, ch = 280, 214
    page = Page(cw)
    bx, by, bw, bh = 24, 24, cw - 48, ch - 48
    page.box(bx, by, bw, bh, weight=1.4, inset=3.5)
    mid = cw / 2
    page.text(mid, by + 40, ad.head, B, 26, anchor="middle")
    page.text(mid, by + 58, ad.sub, B, fit_size(ad.sub, B, bw - 24, 9.5, 7.5, 2), anchor="middle", tracking=2)
    page.rule(mid - 26, by + 67, mid + 26, 0.6)
    lines = clamp_lines(balanced(words_of(ad.body, R), 12, bw - 30), 12, bw - 30, 4)
    for k, ln in enumerate(lines):
        page.line(ln, mid, by + 85 + k * 15.5, 12, anchor="middle")
    size = fit_size(ad.contact, I, bw - 24, 11.5, 8.5)
    page.text(mid, by + bh - 13, ad.contact, I, size, anchor="middle")
    page.sheet(12, 12, cw - 24, ch - 24, seed=60 + n, tone=PAPER_TONES[(n + 2) % len(PAPER_TONES)])
    return page.render(ch, title=f"{ad.head} {ad.sub}", rotate=(0.6, -0.5, 0.3, -0.7)[n % 4])


def classified_ads(ed: dict) -> list[Ad]:
    return [Ad(Path(link).stem.lower(), head, sub, body, contact, link, plain_name)
            for head, sub, body, contact, link, plain_name in ed["classifieds"]["ads"]]


def colophon(ed: dict) -> str:
    col = ed["colophon"]
    page = Page()
    page.text(42, 32, col["left"], B, 9, tracking=1.8)
    page.text(W - 42, 32, col["right"], B, 9, anchor="end", tracking=1.8)
    page.text(W / 2, 32, col["text"], I, fit_size(col["text"], I, W - 260, 11.5, 8.5), anchor="middle")
    page.sheet(12, 8, W - 24, 38, seed=80, tone=PAPER_TONES[2])
    return page.render(56, title="Colophon")


# ── The copy desk: edition.md ───────────────────────────────────────────

@dataclass
class Block:
    kind: str       # h3, h4, p, ul, ol, table, code
    data: object
    source: str     # the Markdown it came from, for the plain-text edition


@dataclass
class Article:
    headline: str
    deck: str = ""
    blocks: list[Block] = field(default_factory=list)


STARTS_BLOCK = re.compile(r"(#|```|\||- |\d+\. |\{\{)")


def parse_edition(text: str) -> list[tuple]:
    """edition.md as a list of ("directive", name), ("banner", title, subtitle) and ("article", Article)."""
    lines = re.sub(r"<!--.*?-->", "", text, flags=re.S).split("\n")
    elements: list[tuple] = []
    art: Article | None = None
    i = 0
    while i < len(lines):
        line, s = lines[i], lines[i].strip()
        if not s:
            i += 1
            continue
        directive = re.fullmatch(r"\{\{(\w+)\}\}", s)
        if directive:
            elements.append(("directive", directive.group(1)))
            art, i = None, i + 1
            continue
        if s.startswith("# "):
            title, _, subtitle = s[2:].partition("|")
            elements.append(("banner", title.strip(), subtitle.strip()))
            art, i = None, i + 1
            continue
        if s.startswith("## "):
            art = Article(s[3:].strip())
            elements.append(("article", art))
            i += 1
            if i < len(lines) and lines[i].startswith("> "):
                art.deck, i = lines[i][2:].strip(), i + 1
            continue
        if art is None:
            raise SystemExit(f"edition.md: text outside an article: {s[:60]!r}")
        if s.startswith("#### ") or s.startswith("### "):
            kind = "h4" if s.startswith("#### ") else "h3"
            art.blocks.append(Block(kind, s.split(" ", 1)[1].strip(), line))
            i += 1
        elif s.startswith("```"):
            j = i + 1
            while not lines[j].startswith("```"):
                j += 1
            art.blocks.append(Block("code", lines[i + 1:j], "\n".join(lines[i:j + 1])))
            i = j + 1
        elif s.startswith("|"):
            j = i
            while j < len(lines) and lines[j].strip().startswith("|"):
                j += 1
            rows = [[c.strip() for c in r.strip().strip("|").split("|")] for r in lines[i:j]]
            art.blocks.append(Block("table", (rows[0], rows[2:]), "\n".join(lines[i:j])))
            i = j
        elif re.match(r"\s*- ", line):
            j, items = i, []
            while j < len(lines) and re.match(r"\s*- ", lines[j]):
                m = re.match(r"(\s*)- (.*)", lines[j])
                items.append((1 + len(m.group(1)) // 2, m.group(2)))
                j += 1
            art.blocks.append(Block("ul", items, "\n".join(lines[i:j])))
            i = j
        elif re.match(r"\d+\. ", s):
            j, items = i, []
            while j < len(lines) and re.match(r"\d+\. ", lines[j].strip()):
                items.append(re.sub(r"^\d+\. ", "", lines[j].strip()))
                j += 1
            art.blocks.append(Block("ol", items, "\n".join(lines[i:j])))
            i = j
        else:
            j = i
            while j < len(lines) and lines[j].strip() and not STARTS_BLOCK.match(lines[j].strip()):
                j += 1
            art.blocks.append(Block("p", " ".join(x.strip() for x in lines[i:j]), "\n".join(lines[i:j])))
            i = j
    return elements


# ── Article pages ───────────────────────────────────────────────────────

TEXT = 12.0             # body type
TEXT_LEAD = 16.4
CODE = 10.6             # code type, before it is fitted to its measure
CODE_LEAD = 14.8
COL_GAP = 28


@dataclass
class Item:
    """One piece of a column: a line, a heading, a code box or a space. draw(page, x, y) sets it at top y."""
    h: float
    draw: object
    keep: bool = False   # never end a column on it (headings stay with their text)
    space: bool = False  # dropped at the top of a column
    opens: bool = True   # the first line of its paragraph (or not a text line at all)
    closes: bool = True  # the last line of its paragraph


def _lines(words: list[Word], width: float, indent=0.0, first=None, justify=True) -> list[Item]:
    broken = break_lines(words, TEXT, lambda _: width - indent, hyphenate=True)
    items = []
    for k, ln in enumerate(broken):
        last = k == len(broken) - 1

        def draw(page, x, y, ln=ln, last=last, k=k):
            base = y + TEXT_LEAD * 0.74
            if k == 0 and first:
                first(page, x, base)
            page.line(ln, x + indent, base, TEXT, justify_to=None if last or not justify else width - indent)
        items.append(Item(TEXT_LEAD, draw, opens=k == 0, closes=last))
    return items


def _space(h: float) -> Item:
    return Item(h, lambda page, x, y: None, space=True)


def code_size(lines: list[str], width: float) -> float:
    longest = max((len(ln) for ln in lines), default=1)
    return min(CODE, (width - 22) / (CHAR * longest))


def _code_box(page: Page, x, y, w, lines: list[str], size: float) -> float:
    lead = size * 1.4
    h = len(lines) * lead + 16
    page.box(x, y, w, h, 0.8)
    for k, ln in enumerate(lines):
        page.code(x + 11, y + 8 + lead * 0.78 + k * lead, ln, size)
    return h


def flow_items(blocks: list[Block], width: float) -> list[Item]:
    """The column pieces of a run of narrow blocks, set to a column `width` wide."""
    items: list[Item] = []
    for b in blocks:
        if b.kind == "h3":
            words = rich(b.data, B)
            if items:
                items.append(_space(10))

            def draw(page, x, y, words=words):
                page.line(words, x, y + 17, 15.5)
                page.rule(x, y + 25, x + 32, 1.4)
            items.append(Item(34, draw, keep=True))
        elif b.kind == "h4":
            words = rich(b.data, B)
            if items:
                items.append(_space(6))
            items.append(Item(20, lambda page, x, y, words=words: page.line(words, x, y + 14, 13.2), keep=True))
        elif b.kind == "p":
            items += _lines(rich(b.data), width)
            items.append(_space(8))
        elif b.kind == "ul":
            for level, text in b.data:
                indent = 15 if level == 1 else 30
                mark = "•" if level == 1 else "–"
                items += _lines(rich(text), width, indent,
                                first=lambda page, x, base, mark=mark, indent=indent: page.text(x + indent - 12, base, mark, R, TEXT))
                items.append(_space(3))
            items.append(_space(5))
        elif b.kind == "ol":
            for n, text in enumerate(b.data, 1):
                items += _lines(rich(text), width, 19,
                                first=lambda page, x, base, n=n: page.text(x, base, f"{n}.", B, TEXT))
                items.append(_space(3))
            items.append(_space(5))
        elif b.kind == "code":
            size = code_size(b.data, width)
            h = len(b.data) * size * 1.4 + 16
            items.append(Item(h + 4, lambda page, x, y, lines=b.data, size=size, w=width: _code_box(page, x, y, w, lines, size)))
            items.append(_space(8))
    while items and items[-1].space:
        items.pop()
    return items


def _split(items: list[Item]) -> int:
    """Where the first column ends, so both columns run about as deep, without a lone line on either side."""
    total = sum(it.h for it in items)
    best, best_k, acc = None, len(items), 0.0
    for k in range(1, len(items)):
        acc += items[k - 1].h
        if items[k - 1].keep or items[k - 1].space:
            continue
        rest = items[k:]
        while rest and rest[0].space:
            rest = rest[1:]
        if not rest:
            continue
        orphan = items[k - 1].opens and not items[k - 1].closes   # a paragraph's first line, left behind
        widow = rest[0].closes and not rest[0].opens               # its last line, carried over alone
        deepest = max(acc, sum(it.h for it in rest)) + (TEXT_LEAD * 2 if orphan or widow else 0)
        if best is None or deepest < best:
            best, best_k = deepest, k
    if best is None or best >= total:
        return len(items)
    return best_k


def _set_column(page: Page, items: list[Item], x, y) -> float:
    while items and items[0].space:
        items = items[1:]
    top = y
    for it in items:
        it.draw(page, x, y)
        y += it.h
    return y - top


def set_flow(page: Page, blocks: list[Block], x0, y, width) -> float:
    """Sets a run of text in two balanced columns (or across the page, if it is short). Returns the new y."""
    whole = flow_items(blocks, width)
    if sum(it.h for it in whole) <= 3.5 * TEXT_LEAD:
        return y + _set_column(page, whole, x0, y) + 6
    colw = (width - COL_GAP) / 2
    items = flow_items(blocks, colw)
    k = _split(items)
    left = _set_column(page, items[:k], x0, y)
    right = _set_column(page, items[k:], x0 + colw + COL_GAP, y)
    depth = max(left, right)
    page.vrule(x0 + colw + COL_GAP / 2, y + 2, y + depth - 4)
    return y + depth + 6


def set_table(page: Page, header: list[str], rows: list[list[str]], x0, y, width) -> float:
    """A table set like a newspaper's listings: small-caps heads, dotted rows, columns sized to their text."""
    size, lead, pad = 11.2, 15.0, 7
    cells = [[rich(c) for c in row] for row in rows]
    n = len(header)
    longest = [max((f.width(w, size) for row in cells for w, f in row[c]), default=30) for c in range(n)]
    heads = [words_of(h.upper(), B) for h in header]
    head_word = [max(B.width(w, 8.8, 1.3) for w, _ in hd) for hd in heads]
    mins = [max(lw, hw) + 2 * pad for lw, hw in zip(longest, head_word)]
    natural = [sum(line_width(row[c], size) for row in cells) / max(1, len(cells)) for c in range(n)]
    weights = [max(v, 10) ** 0.8 for v in natural]
    spare = width - sum(mins)
    if spare > 0:
        widths = [m + spare * wt / sum(weights) for m, wt in zip(mins, weights)]
    else:
        widths = [m * width / sum(mins) for m in mins]
    xs = [x0 + sum(widths[:c]) for c in range(n)]

    def wrap_head(words: list[str], measure: float) -> list[str]:  # tracked caps, measured with their tracking
        lines = [""]
        for w in words:
            trial = f"{lines[-1]} {w}".strip()
            if lines[-1] and B.width(trial, 8.8, 1.3) > measure:
                lines.append(w)
            else:
                lines[-1] = trial
        return lines

    head_lines = [wrap_head([w for w, _ in hd], widths[c] - 2 * pad) for c, hd in enumerate(heads)]
    hh = max(len(hl) for hl in head_lines) * 11.5
    page.rule(x0, y, x0 + width, 1.4)
    for c, hl in enumerate(head_lines):
        for k, ln in enumerate(hl):
            page.text(xs[c] + pad, y + 14 + k * 11.5, ln, B, 8.8, tracking=1.3)
    y += hh + 9
    page.rule(x0, y, x0 + width, 0.8)
    y += 4
    for r, row in enumerate(cells):
        broken = [break_lines(row[c], size, lambda _, c=c: widths[c] - 2 * pad, hyphenate=True) for c in range(n)]
        h = max(len(b) for b in broken) * lead
        for c, b in enumerate(broken):
            for k, ln in enumerate(b):
                page.line(ln, xs[c] + pad, y + lead * 0.78 + k * lead, size)
        y += h + 7
        if r < len(cells) - 1:
            page.rule(x0, y - 3, x0 + width, 0.6, dash="0.6 2.6")
    page.rule(x0, y, x0 + width, 1.4)
    return y + 18


def is_wide(b: Block, colw: float) -> bool:
    if b.kind == "table":
        return True
    return b.kind == "code" and code_size(b.data, colw) < 9.4


def article_page(art: Article, section: str, folio: str, seed: int) -> str:
    page = Page()
    x0, x1 = 40, W - 40
    cw, mid = x1 - x0, W / 2
    page.text(x0, 42, section, B, 9.5, tracking=2.2)
    page.text(x1, 42, folio, B, 9.5, anchor="end", tracking=2.2)
    page.rule(x0, 51, x1, 0.9)
    y = headline(page, mid, 66, art.headline.upper(), cw, 32, 22) + 20
    if art.deck:
        y = centred_deck(page, mid, y, art.deck, cw * 0.84, 15) + 20
    page.rule(x0, y, x1, 0.8)
    page.rule(x0, y + 3.8, x1, 2.4)
    y += 28

    colw = (cw - COL_GAP) / 2
    run: list[Block] = []
    for b in art.blocks + [None]:
        if b is not None and not is_wide(b, colw):
            run.append(b)
            continue
        heads = []
        while b is not None and run and run[-1].kind in ("h3", "h4"):  # a subhead goes above its table, not beside it
            heads.insert(0, run.pop())
        if run:
            y = set_flow(page, run, x0, y, cw)
            run = []
        if heads:
            y = set_flow(page, heads, x0, y, cw) - 2
        if b is None:
            break
        if b.kind == "table":
            y = set_table(page, *b.data, x0, y + 2, cw)
        else:
            size = code_size(b.data, cw)
            y += _code_box(page, x0, y + 2, cw, b.data, size) + 16
    page.sheet(12, 12, W - 24, y - 12, seed=seed, tone=PAPER_TONES[seed % len(PAPER_TONES)])
    return page.render(y + 14, title=f"{art.headline}. {art.deck}".strip(". "))


# ── README: the pages in order, then the plain-text edition ─────────────

def attr(s: str) -> str:
    return html.escape(s, quote=True)


def picture(name: str, width: str, alt: str, href: str | None = None) -> str:
    """A page that GitHub swaps for its night edition under the dark theme."""
    tag = (f'<picture><source media="(prefers-color-scheme: dark)" srcset="docs/readme/dark/{name}">'
           f'<img src="docs/readme/{name}" width="{width}" alt="{attr(alt)}"></picture>')
    return f'<a href="{attr(href)}">{tag}</a>' if href else tag


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


SMALL_WORDS = {"a", "an", "and", "the", "of", "in", "on", "to", "for", "from", "with"}


def heading_case(caps: str) -> str:
    """BEFORE YOU BEGIN → Before You Begin, keeping small words small."""
    words = caps.lower().split()
    return " ".join(w if n and w in SMALL_WORDS else w[:1].upper() + w[1:] for n, w in enumerate(words))


def lower_first(s: str) -> str:
    """Lower-cases a sentence's first letter, but not a word that is capitals throughout (SQLite, AES)."""
    return s if len(s) > 1 and s[1].isupper() else s[:1].lower() + s[1:]


def plain_front(ed: dict) -> list[str]:
    paper, lead, staff = ed["paper"], ed["lead"], ed["staff"]
    return [
        f"### {paper['title']}",
        f"*{paper['motto']}* · {paper['plain_line']}",
        "",
        f"#### {lead['headline']}",
        f"*{lead['deck']}.*",
        "",
        f"**{lead['byline']}**",
        "",
        *[line for para in lead["story"] for line in (para, "")],
        "**On the staff:** " + " · ".join(f"{m['name']} ({m['beat']})" for m in staff["members"]),
        "",
        "**The figures:** " + " · ".join(ed["figures"]["items"]),
        "",
    ]


def plain_architecture(ed: dict) -> list[str]:
    a = ed["architecture"]
    label = lambda caps: caps.split("· ")[-1].capitalize()  # "2 · THE APP" → "The app"
    out = [f"**{a['headline']}** {a['deck']}.", ""]
    out.append("- **What comes in:** " + "; ".join(f"{lower_first(i['title'])} ({i['body']})" for i in a["inputs"]) + ".")
    out.append(f"- **{label(a['window']['label'])}:** {a['window']['title']}. {a['window']['body']}")
    out.append(f"- **{label(a['app_label'])}** ({a['app_note']}): "
               + "; ".join(f"{lower_first(d['title'])} (`{d['files'].split(' ')[0]}`): {lower_first(d['body']).rstrip('.')}"
                           for d in a["desks"]) + ".")
    out.append(f"- **{label(a['models_label'])}:** " + " ".join(f"{m['title']}: {m['body']}" for m in a["models"]))
    out.append(f"- **{label(a['doors_title'])}:** {a['doors']}")
    return out + [""]


def plain_pipelines(ed: dict) -> list[str]:
    out = []
    for strip in ed["pipelines"]["lines"]:
        out.append(f"**{strip['title'].capitalize()}:** "
                   + " → ".join(f"{t.capitalize()} ({lower_first(b).rstrip('.')})" for t, b in strip["steps"]) + ".")
        out.append("")
    return out


def plain_edition(ed: dict, elements: list[tuple]) -> list[str]:
    out: list[str] = []
    for el in elements:
        if el[0] == "banner":
            out += ["---", "", f"## {heading_case(el[1])}", f"*{el[2]}*", ""]
        elif el[0] == "article":
            art = el[1]
            out += [f"### {art.headline}", ""] + ([f"*{art.deck}.*", ""] if art.deck else [])
            for b in art.blocks:
                src = b.source
                if b.kind in ("h3", "h4"):
                    src = "#" + src.lstrip()
                out += [src, ""]
        elif el[1] == "front":
            out += plain_front(ed)
        elif el[1] == "newsreel":
            out += [f"**The newsreel** ({ed['newsreel']['kicker'].split('· ')[1].lower()}): {ed['newsreel']['caption']}", ""]
        elif el[1] == "architecture":
            out += plain_architecture(ed)
        elif el[1] == "pipelines":
            out += plain_pipelines(ed)
        elif el[1] == "classifieds":
            out += ["**Classifieds:** " + " · ".join(f"[{ad.plain}]({ad.link})" for ad in classified_ads(ed)), ""]
    while out and not out[-1]:
        out.pop()
    return out


# ── Main ────────────────────────────────────────────────────────────────

def main():
    ed = tomllib.loads((PRESS / "edition.toml").read_text(encoding="utf-8"))
    elements = parse_edition((PRESS / "edition.md").read_text(encoding="utf-8"))
    ads = classified_ads(ed)
    pages: dict[str, str] = {}
    layout: list[list[str]] = []  # README rows: each row a list of <picture> tags, side by side
    gif = (OUT / "newsreel.gif").exists() and (OUT / "dark" / "newsreel.gif").exists()

    section, letter, number, seed = "", "A", 1, 100
    for el in elements:
        if el[0] == "banner":
            _, title, subtitle = el
            name = f"section-{slug(title)}.svg"
            pages[name] = section_banner(title, subtitle, 90 + len(layout))
            layout.append([picture(name, "100%", f"{heading_case(title)}: {subtitle}")])
            section, letter, number = title, chr(ord(letter) + 1), 0
        elif el[0] == "article":
            number += 1
            seed += 1
            art = el[1]
            name = f"page-{letter.lower()}{number}.svg"
            pages[name] = article_page(art, section, f"PAGE {letter}{number}", seed)
            alt = f"Page {letter}{number}: {art.headline}. {art.deck}. The full text is in the plain-text edition below."
            layout.append([picture(name, "100%", alt)])
        elif el[1] == "front":
            pages["front-page.svg"] = front_page(ed)
            lead = ed["lead"]
            layout.append([picture("front-page.svg", "100%", f"{ed['paper']['title']}. {ed['paper']['motto']} "
                                   f"Lead story: {lead['headline']}. {lead['deck']}.")])
        elif el[1] == "newsreel":
            pages["newsreel.svg"] = newsreel(ed)
            layout.append([picture("newsreel.gif" if gif else "newsreel.svg", "100%", ed["newsreel"]["alt"])])
        elif el[1] in ("architecture", "pipelines"):
            pages[f"{el[1]}.svg"] = (architecture if el[1] == "architecture" else pipelines)(ed)
            plain_text = (plain_architecture if el[1] == "architecture" else plain_pipelines)(ed)
            layout.append([picture(f"{el[1]}.svg", "100%", " ".join(plain_text).replace("**", "").replace("`", ""))])
        elif el[1] == "classifieds":
            row = []
            for n, ad in enumerate(ads):
                pages[f"ad-{ad.slug}.svg"] = ad_clipping(ad, n)
                row.append(picture(f"ad-{ad.slug}.svg", "32%", f"{ad.plain}: {ad.body} {ad.contact}", ad.link))
            layout.append(row)
        elif el[1] == "colophon":
            pages["colophon.svg"] = colophon(ed)
            layout.append([picture("colophon.svg", "100%", ed["colophon"]["text"])])
        else:
            raise SystemExit(f"edition.md: unknown page {{{{{el[1]}}}}}")

    (OUT / "dark").mkdir(parents=True, exist_ok=True)
    for stale in [*OUT.glob("*.svg"), *(OUT / "dark").glob("*.svg")]:
        if stale.name not in pages:
            stale.unlink()
    for name, svg in pages.items():
        (OUT / name).write_text(svg, encoding="utf-8")
        (OUT / "dark" / name).write_text(night_edition(svg), encoding="utf-8")

    out = ["<!-- Printed by docs/press/press.py from docs/press/edition.md and edition.toml. "
           "Edit those and run the press: it overwrites this file. -->",
           '<div align="center">', ""]
    for row in layout:
        out += ["\n".join(row), ""]
    out += ["</div>", "", "<details>",
            "<summary><b>Read the plain-text edition</b> (for screen readers, small screens, search engines and "
            "copying commands)</summary>", "", *plain_edition(ed, elements), "", "</details>", ""]
    README.write_text("\n".join(out), encoding="utf-8")
    print(f"printed {len(pages)} pages to {OUT.relative_to(ROOT).as_posix()}/ (and dark/), and README.md"
          f"{'' if gif else '; the newsreel is still: run newsreel.py to set it moving'}")


if __name__ == "__main__":
    main()
