"""Draw the application icon, so it is reproducible rather than a binary nobody can edit.

The tray icon is a plain disc whose colour carries the state, which is right for a 16px
status area but says nothing on a Start Menu tile. This is the same green on a dark disc
with a reticle through it, which at least looks like something that points at the sky.

Run it after changing the colours::

	python packaging/make_icon.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from onstep_alpaca.tray import CONNECTED  # noqa: E402

DISC = (0x1B, 0x24, 0x30)
MARK = CONNECTED
"""The same green the tray icon uses when the mount is connected, so the two read as one
program rather than two."""

SIZES = [16, 24, 32, 48, 64, 128, 256]
SUPERSAMPLE = 8
"""Drawn large and shrunk with LANCZOS: PIL has no antialiasing, and a 16px reticle drawn
directly is a staircase."""


def draw(size: int) -> Image.Image:
	big = size * SUPERSAMPLE
	image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
	pen = ImageDraw.Draw(image)

	pen.ellipse((0, 0, big - 1, big - 1), fill=DISC + (255,))

	ring = big * 0.21
	stroke = max(2, round(big * 0.055))
	centre = big / 2
	pen.ellipse((centre - ring, centre - ring, centre + ring, centre + ring),
	            outline=MARK + (255,), width=stroke)

	# Crosshair arms, stopping short of the ring so the centre stays open.
	gap, reach = ring * 1.45, big * 0.40
	for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
		pen.line((centre + dx * gap, centre + dy * gap,
		          centre + dx * reach, centre + dy * reach),
		         fill=MARK + (255,), width=stroke)

	return image.resize((size, size), Image.LANCZOS)


def main() -> int:
	target = Path(__file__).resolve().parent / "onstep-alpaca.ico"
	frames = [draw(size) for size in SIZES]
	frames[-1].save(target, format="ICO", sizes=[(s, s) for s in SIZES])
	print(f"wrote {target} ({target.stat().st_size} bytes, sizes {SIZES})")
	return 0


if __name__ == "__main__":
	sys.exit(main())
