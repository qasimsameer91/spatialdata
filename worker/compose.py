"""Generates the attribution overlay and end-credit card burned into the video.

Attribution is a licence obligation for every data source this pipeline uses,
so it is produced automatically rather than left to the operator to remember:

* a persistent, unobtrusive strip in the corner of every frame
* a full-screen credit card at the end listing each source

Both are rendered to PNG with Pillow and composited by FFmpeg, which avoids
FFmpeg's `drawtext` filter entirely - its font-path escaping on Windows is a
reliable source of silent failures.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw, ImageFont

from .log import get as get_logger

log = get_logger("compose")

# Preference order; the first that loads wins. Bold variants are paired.
_FONT_CANDIDATES = [
    ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/segoeuib.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("C:/Windows/Fonts/calibri.ttf", "C:/Windows/Fonts/calibrib.ttf"),
]

DEFAULT_ATTRIBUTION = (
    "Map data (c) OpenStreetMap contributors, OpenFreeMap. "
    "Satellite imagery: NASA GIBS/EOSDIS. "
    "Population data: Kontur Population (CC BY). "
    "Boundaries: Natural Earth."
)

# The full notice is too long to sit over the picture for the whole video, so
# the persistent strip carries this compact form and the end card carries the
# full text. Every source is still named on screen throughout.
SHORT_ATTRIBUTION = (
    "© OpenStreetMap · OpenFreeMap · Mapterhorn · "
    "NASA GIBS · Kontur · Natural Earth"
)

# Credit lines, keyed by the pipeline feature that requires them. Only the
# ones a given video actually used are shown: crediting NASA GIBS on a clip
# with no satellite imagery is both noise and a false statement about where
# the pictures came from.
CREDIT_SOURCES: dict[str, tuple[str, str]] = {
    "basemap": ("Basemap", "OpenStreetMap contributors / OpenFreeMap"),
    "terrain": ("Terrain", "Mapterhorn"),
    "satellite": ("Satellite imagery", "NASA GIBS / EOSDIS"),
    "weather": ("Weather layers", "NASA GIBS / EOSDIS"),
    "population": ("Population", "Kontur Population (CC BY)"),
    "borders": ("Boundaries", "Natural Earth"),
    "music": ("Music", "Kevin MacLeod (CC BY 4.0)"),
}


def credits_for(sources: Sequence[str]) -> list[tuple[str, str]]:
    """Credit lines for the features this render actually used, de-duplicated."""
    out: list[tuple[str, str]] = []
    for key in sources:
        line = CREDIT_SOURCES.get(key)
        if line and line not in out:
            out.append(line)
    return out


def _load_fonts(size: int) -> tuple[ImageFont.FreeTypeFont, ImageFont.FreeTypeFont]:
    for regular, bold in _FONT_CANDIDATES:
        try:
            return (ImageFont.truetype(regular, size),
                    ImageFont.truetype(bold, size))
        except OSError:
            continue
    log.warning("no TrueType font found; falling back to the bitmap default")
    fallback = ImageFont.load_default()
    return fallback, fallback


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int,
          draw: ImageDraw.ImageDraw) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def attribution_strip(width: int, height: int, text: str = SHORT_ATTRIBUTION,
                      *, out_path: Path | None = None) -> Path:
    """A transparent PNG the size of the frame with attribution in the corner.

    Sized to stay on one line: a wrapped block in the corner of every frame
    reads as a mistake rather than a credit. If the caller passes text too long
    to fit, the type shrinks until it does.
    """
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    margin = round(height * 0.022)
    max_text_width = width - 2 * margin - round(width * 0.04)

    size = max(10, round(height * 0.0135))
    font, _ = _load_fonts(size)
    while size > 9 and draw.textlength(text, font=font) > max_text_width:
        size -= 1
        font, _ = _load_fonts(size)

    lines = _wrap(text, font, max_text_width, draw)
    line_h = size + round(size * 0.35)
    pad_x = round(size * 0.85)
    pad_y = round(size * 0.5)
    text_w = max(draw.textlength(l, font=font) for l in lines)
    block_w = text_w + pad_x * 2
    block_h = line_h * len(lines) + pad_y * 2

    x0 = width - margin - block_w
    y0 = height - margin - block_h

    # A soft plate keeps the text legible over both bright and dark terrain.
    draw.rounded_rectangle(
        [x0, y0, x0 + block_w, y0 + block_h],
        radius=round(size * 0.55), fill=(0, 0, 0, 105),
    )
    for i, line in enumerate(lines):
        draw.text((x0 + pad_x, y0 + pad_y + i * line_h), line,
                  font=font, fill=(255, 255, 255, 190))

    out_path = Path(out_path) if out_path else Path("attribution.png")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path


def end_card(width: int, height: int, *, title: str = "",
             lines: Sequence[tuple[str, str]] = (),
             subtitle: str = "", out_path: Path | None = None) -> Path:
    """Closing card: the title, then the sources this video actually used.

    Deliberately not black. A near-black card cutting in after the picture
    reads as the video having broken rather than having ended, so the ground
    is a soft dark slate with a gradient, in the same family as the flyover
    frames it follows.
    """
    lines = list(lines) if lines else credits_for(["basemap", "terrain"])
    img = Image.new("RGB", (width, height), (17, 22, 31))
    draw = ImageDraw.Draw(img)

    unit = height / 1080.0
    label_font, _ = _load_fonts(round(21 * unit))
    value_font, _ = _load_fonts(round(25 * unit))
    _, title_font = _load_fonts(round(52 * unit))
    small_font, _ = _load_fonts(round(19 * unit))

    # Vertical gradient, lighter at the top, so the card has some depth
    # instead of reading as a dropout to black.
    for y in range(height):
        t = y / max(1, height - 1)
        draw.line([(0, y), (width, y)],
                  fill=(int(26 - 12 * t), int(32 - 14 * t), int(44 - 18 * t)))

    block_h = (len(lines) * 52 * unit) + (150 * unit if title else 0)
    y = (height - block_h) / 2

    if title:
        draw.text((width / 2, y), title, font=title_font,
                  fill=(244, 248, 253), anchor="mm")
        y += 62 * unit
        if subtitle:
            draw.text((width / 2, y), subtitle, font=small_font,
                      fill=(138, 158, 182), anchor="mm")
            y += 34 * unit
        # A short rule under the title, the way a film card sets off its name.
        rule = width * 0.05
        draw.line([(width / 2 - rule, y), (width / 2 + rule, y)],
                  fill=(74, 92, 114), width=max(1, round(unit)))
        y += 58 * unit

    row_h = 52 * unit
    for i, (label, value) in enumerate(lines):
        ry = y + i * row_h
        draw.text((width * 0.5 - 26 * unit, ry), label, font=label_font,
                  fill=(126, 146, 172), anchor="rm")
        draw.text((width * 0.5 + 26 * unit, ry), value, font=value_font,
                  fill=(228, 237, 248), anchor="lm")

    out_path = Path(out_path) if out_path else Path("endcard.png")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)
    return out_path
