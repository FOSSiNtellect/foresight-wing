#!/usr/bin/env python3
"""The moving newsreel: the printed newsreel page, with the demo playing in its photo.

    python docs/press/newsreel.py path/to/demo.mp4

Prints docs/readme/newsreel.gif (day) and docs/readme/dark/newsreel.gif (night), then runs the press so the README
shows them. Needs ffmpeg and Microsoft Edge, which prints the page itself. Which part of the video plays, how fast
and at how many frames a second is set in edition.toml's [newsreel].
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import press  # noqa: E402

EDGE = (Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"))
# What sits behind the sheet: GitHub's own page colour, since a GIF cannot fade a shadow into any background.
BACKDROP = {"day": "#ffffff", "night": "#0d1117"}


def print_page(svg: str, backdrop: str, png: Path, width: int, height: int, edge: str):
    page = png.with_suffix(".svg")
    page.write_text(svg.replace("</style>", f'</style><rect width="100%" height="100%" fill="{backdrop}"/>', 1),
                    encoding="utf-8")
    subprocess.run([edge, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
                    f"--window-size={width},{height}", f"--screenshot={png}", page.as_uri()],
                   check=True, capture_output=True, timeout=120)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("video", type=Path, help="the demo video (MP4)")
    args = parser.parse_args()
    edge = next((str(p) for p in EDGE if p.is_file()), None) or shutil.which("msedge")
    ffmpeg = shutil.which("ffmpeg")
    if not edge or not ffmpeg:
        sys.exit("newsreel.py needs Microsoft Edge and ffmpeg")

    ed = tomllib.loads((press.PRESS / "edition.toml").read_text(encoding="utf-8"))
    reel = ed["newsreel"]
    svg = press.newsreel(ed)
    x, y, w, h = (int(v) for v in press.NEWSREEL_PHOTO)
    width, height = (int(float(v)) for v in re.search(r'viewBox="0 0 (\S+) (\S+)"', svg).groups())
    start, length = reel["clip"]
    speed, fps = reel["speed"], reel["fps"]
    (press.OUT / "dark").mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        for edition, page_svg, tone, out in (
                ("day", svg, "negate,", press.OUT / "newsreel.gif"),  # a dark screenshot prints light on day paper
                ("night", press.night_edition(svg), "", press.OUT / "dark" / "newsreel.gif")):
            png = Path(tmp) / f"{edition}.png"
            print_page(page_svg, BACKDROP[edition], png, width, height, edge)
            graph = (f"[1:v]setpts=PTS/{speed},fps={fps},scale={w - 2}:{h - 2}:flags=lanczos,format=gray,"
                     f"{tone}eq=contrast=1.06,format=rgb24[film];"
                     f"[0:v]crop={width}:{height}:0:0[page];[page][film]overlay={x + 1}:{y + 1}:shortest=1,split[a][b];"
                     "[a]palettegen=max_colors=160:stats_mode=full[ink];"
                     "[b][ink]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle")
            subprocess.run([ffmpeg, "-v", "error", "-y", "-loop", "1", "-framerate", str(fps), "-i", str(png),
                            "-ss", str(start), "-t", str(length), "-i", str(args.video),
                            "-filter_complex", graph, "-loop", "0", str(out)], check=True)
            print(f"{out.relative_to(press.ROOT).as_posix()}: {out.stat().st_size / 1e6:.1f} MB")
    press.main()


if __name__ == "__main__":
    main()
