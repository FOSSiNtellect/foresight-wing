#!/usr/bin/env python3
"""The Foresight Wing press: prints the newspaper pages that README.md shows.

A special edition from the FOSSiNtellect presses (github.com/FOSSiNtellect), set in the same type. It reads
edition.toml and prints docs/readme/*.svg, plus a night edition of each page for GitHub's dark theme in
docs/readme/dark/. README.md itself is written by hand. Standard library only, so it runs anywhere Python 3.11+ does.

    python docs/press/press.py
"""
from __future__ import annotations

import base64
import json
import math
import random
import tomllib
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[2]
PRESS = ROOT / "docs" / "press"
FONTS = PRESS / "fonts"
OUT = ROOT / "docs" / "readme"

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
    '.h{font-family:Caveat,"Segoe Print","Bradley Hand",cursive;font-kerning:normal}' % INK
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

    With hyphenate, a word that would leave a loose line is split across it.
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
            if hyphenate and limit - used > space * 1.6:
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
            spans.append(f'<tspan x="{x:.1f}" class="{f.css}">{escape(w)}</tspan>')
            x += ww + gap
        color = f' fill="{fill}"' if fill != INK else ""
        self.add(f'<text y="{y:.1f}" font-size="{size:g}"{color}>{"".join(spans)}</text>')

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

    def play_button(self, cx, cy, r):
        self.add(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="{INK}" fill-opacity=".86"/>')
        self.add(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r - 4:.1f}" fill="none" stroke="{PAPER_TONES[0]}" stroke-width="1.2"/>')
        s = r * 0.46
        pts = [(cx - s * 0.7, cy - s), (cx - s * 0.7, cy + s), (cx + s, cy)]
        self.add(f'<polygon points="{" ".join(f"{px:.1f},{py:.1f}" for px, py in pts)}" fill="{PAPER_TONES[0]}"/>')

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
        for key in sorted(self.used):
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


# ── Inside pages ────────────────────────────────────────────────────────

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


def newsreel(ed: dict) -> str:
    reel, notes = ed["newsreel"], ed.get("notes", {})
    page = Page()
    x0, x1 = 40, W - 40
    cw, mid = x1 - x0, W / 2
    page.text(x0, 46, reel["kicker"], B, 10, fill=ACCENT, tracking=2.4)
    page.text(x1, 46, reel["label"], B, 9.5, anchor="end", tracking=1.6)
    page.rule(x0, 55, x1, 0.9)
    y = headline(page, mid, 68, reel["headline"], cw, 30, 22) + 18

    ph = cw * 9 / 16
    page.photo(x0, y, cw, ph, PRESS / "newsreel.jpg", night=PRESS / "newsreel-night.jpg")
    page.box(x0, y, cw, ph, 1.4)
    page.box(x0 - 4, y - 4, cw + 8, ph + 8, 0.6)
    page.play_button(mid, y + ph / 2 - 20, 38)
    if notes.get("newsreel"):  # the player itself sits just under this page in the README
        nw = page.note(mid + 70, y + ph / 2 + 58, notes["newsreel"], 26, tilt=-4)
        page.arrow(mid + 70 + nw * 0.6, y + ph / 2 + 66, mid + 70 + nw * 0.75, y + ph - 14, bend=-0.25)
    y += ph + 12
    page.text(x1, y + 6, f"PHOTO: {reel['credit']}", B, 8.5, anchor="end", fill=INK_SOFT, tracking=1.2)
    y += 26
    lines = break_lines(words_of(reel["caption"], I), 13.5, lambda _: cw, hyphenate=True)
    for k, ln in enumerate(lines):
        page.line(ln, x0, y + k * 18.5, 13.5, justify_to=None if k == len(lines) - 1 else cw)
    y += (len(lines) - 1) * 18.5 + 18
    page.rule(x0, y, x1, 0.6)
    page.text(x0, y + 18, reel["footer"], I, 12, fill=INK_SOFT)
    page.text(x1, y + 18, reel["action"], B, 10, anchor="end", tracking=1.6)
    y += 34
    page.sheet(16, 14, W - 32, y - 14 + 8, seed=30, tone=PAPER_TONES[1])
    return page.render(y + 22, title=reel["headline"], rotate=-0.3)


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


def colophon(ed: dict) -> str:
    col = ed["colophon"]
    page = Page()
    page.text(42, 32, col["left"], B, 9, tracking=1.8)
    page.text(W - 42, 32, col["right"], B, 9, anchor="end", tracking=1.8)
    page.text(W / 2, 32, col["text"], I, fit_size(col["text"], I, W - 260, 11.5, 8.5), anchor="middle")
    page.sheet(12, 8, W - 24, 38, seed=80, tone=PAPER_TONES[2])
    return page.render(56, title="Colophon")


# ── Main ────────────────────────────────────────────────────────────────

def main():
    ed = tomllib.loads((PRESS / "edition.toml").read_text(encoding="utf-8"))
    pages = {"front-page.svg": front_page(ed), "newsreel.svg": newsreel(ed),
             "architecture.svg": architecture(ed), "pipelines.svg": pipelines(ed), "colophon.svg": colophon(ed)}
    for n, (slug, (title, subtitle)) in enumerate(ed["sections"].items()):
        pages[f"section-{slug}.svg"] = section_banner(title, subtitle, 90 + n)
    for n, (head, sub, body, contact, link) in enumerate(ed["classifieds"]["ads"]):
        slug = Path(link).stem.lower()
        pages[f"ad-{slug}.svg"] = ad_clipping(Ad(slug, head, sub, body, contact), n)
    (OUT / "dark").mkdir(parents=True, exist_ok=True)
    for name, svg in pages.items():
        (OUT / name).write_text(svg, encoding="utf-8")
        (OUT / "dark" / name).write_text(night_edition(svg), encoding="utf-8")
    print(f"printed {len(pages)} pages to {OUT.relative_to(ROOT).as_posix()}/ (and dark/)")


if __name__ == "__main__":
    main()
