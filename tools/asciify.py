#!/usr/bin/env python3
"""Convert a logo image into the quarter-block ASCII art that today.py renders.

Not part of the build: ascii.txt is committed, and this only needs re-running when
the source logo changes. Unlike today.py it is NOT stdlib-only -- it needs Pillow
(pip install pillow), which is why it lives here instead of in the render path.

    python3 tools/asciify.py path/to/logo.png            # preview
    python3 tools/asciify.py path/to/logo.png --write    # overwrite ascii.txt + ascii.cov
    python3 tools/asciify.py path/to/logo.png --cols=36  # narrower art

Each character carries a 2x2 grid of subsamples via the quadrant glyphs, so the
sample cell is CHAR_W/2 x LINE_H/2 -- twice the horizontal resolution of a
half-block encoder, which is where diagonals were stair-stepping.

Two files come out, and they must stay the same shape:

    ascii.txt   the glyphs
    ascii.cov   one digit per glyph, how completely that cell's ink covers it

The digit is what lets today.py soften edge cells. It cannot be recovered from the
glyph afterwards: a quadrant glyph already paints the right *area*, so an edge cell
and a solid cell can carry the identical glyph while covering very different
fractions of their source pixels.
"""

import os
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from today import ASCII_COLS, CHAR_W, LINE_H  # noqa: E402  (grid constants, single source)

INK = 0.5        # a quadrant is ink when this much of it is covered
MARGIN = 1       # columns of gutter kept on the left
COLS = 40        # ASCII_COLS is the hard ceiling; 40 leaves a little air

# (top-left, top-right, bottom-left, bottom-right) -> glyph
QUADRANTS = {
    (0, 0, 0, 0): " ", (1, 0, 0, 0): "▘", (0, 1, 0, 0): "▝", (1, 1, 0, 0): "▀",
    (0, 0, 1, 0): "▖", (1, 0, 1, 0): "▌", (0, 1, 1, 0): "▞", (1, 1, 1, 0): "▛",
    (0, 0, 0, 1): "▗", (1, 0, 0, 1): "▚", (0, 1, 0, 1): "▐", (1, 1, 0, 1): "▜",
    (0, 0, 1, 1): "▄", (1, 0, 1, 1): "▙", (0, 1, 1, 1): "▟", (1, 1, 1, 1): "█",
}


def asciify(path, cols):
    """Return (glyph_lines, coverage_lines), both rstripped to the same widths."""
    img = Image.open(path).convert("L")

    # Crop to the ink bounding box first: source logos are mostly padding, and
    # padding spent here is columns not spent on the letterforms.
    box = img.point(lambda p: 255 if p < 128 else 0).getbbox()
    if box is None:
        sys.exit("no ink found in " + path)
    img = img.crop(box)
    w, h = img.size

    # Two subsamples across per character, two down. Sub-columns are CHAR_W/2
    # wide and sub-rows LINE_H/2 tall, so preserving the source aspect means
    # sizing the sub-row count against that cell, not against a square one.
    sub_cols = cols * 2
    sub_rows = round(sub_cols * (h / w) * ((CHAR_W / 2) / (LINE_H / 2)))
    sub_rows += sub_rows % 2  # even, so every text row gets both of its subsamples

    # BOX averages over each destination cell, which is exactly "what fraction of
    # this cell is ink". LANCZOS would ring at the letterform edges and invent ink.
    px = img.resize((sub_cols, sub_rows), Image.BOX).load()

    glyphs, covers = [], []
    for r in range(0, sub_rows, 2):
        grow, crow = [], []
        for c in range(0, sub_cols, 2):
            quad = [
                (255 - px[c, r]) / 255.0, (255 - px[c + 1, r]) / 255.0,
                (255 - px[c, r + 1]) / 255.0, (255 - px[c + 1, r + 1]) / 255.0,
            ]
            bits = tuple(1 if q >= INK else 0 for q in quad)
            grow.append(QUADRANTS[bits])
            lit = [q for q, b in zip(quad, bits) if b]
            if not lit:
                crow.append("0")
            else:
                # Ink coverage lands in [INK, 1]; stretch that to 0-9 so the whole
                # digit range is used and today.py gets a real gradation to work with.
                mean = sum(lit) / len(lit)
                crow.append(str(max(0, min(9, int(round((mean - INK) / (1 - INK) * 9))))))
        pad = " " * MARGIN
        glyph_line = (pad + "".join(grow)).rstrip()
        cover_line = (pad + "".join(crow))[:len(glyph_line)]
        glyphs.append(glyph_line)
        covers.append(cover_line)

    while glyphs and not glyphs[0]:
        glyphs.pop(0)
        covers.pop(0)
    while glyphs and not glyphs[-1]:
        glyphs.pop()
        covers.pop()
    return glyphs, covers


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    write = "--write" in sys.argv
    cols = next((int(a.split("=")[1]) for a in sys.argv[1:] if a.startswith("--cols=")), COLS)

    glyphs, covers = asciify(args[0], cols)
    widest = max(len(line) for line in glyphs)
    if widest > ASCII_COLS:
        sys.exit("art is %d cols wide, panel starts at %d" % (widest, ASCII_COLS))
    for g, c in zip(glyphs, covers):
        assert len(g) == len(c), "glyph/coverage shape drift: %r vs %r" % (g, c)

    # Leading blank line drops the art one row, level with the panel's first entry.
    art = "\n" + "\n".join(glyphs) + "\n"
    cov = "\n" + "\n".join(covers) + "\n"
    if write:
        with open(os.path.join(ROOT, "ascii.txt"), "w", encoding="utf-8") as fh:
            fh.write(art)
        with open(os.path.join(ROOT, "ascii.cov"), "w", encoding="utf-8") as fh:
            fh.write(cov)
        print("wrote ascii.txt and ascii.cov")
    else:
        sys.stdout.write(art)
    print("%d cols x %d rows (max %d)" % (widest, len(glyphs), ASCII_COLS), file=sys.stderr)


if __name__ == "__main__":
    main()
