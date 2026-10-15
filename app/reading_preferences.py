"""Shared reading preference contract; legacy integer sizes remain valid."""
from __future__ import annotations
import math

FONT_FAMILIES = ('system', 'misans', 'noto-sans-sc', 'lxgw-wenkai', 'chill-round')
MIN_FONT_SIZE = 12
MAX_FONT_SIZE = 32

def valid_font_size(value):
    # bool is an int subclass; reject it and non-finite decoder values explicitly.
    return (type(value) in (int, float) and MIN_FONT_SIZE <= value <= MAX_FONT_SIZE
            and math.isfinite(value)
            and value * 2 == int(value * 2))


# Exact reviewed application assets. Updating a font requires a new source pin;
# request data or an untrusted manifest cannot expand the immutable cache set.
FONT_ASSET_PINS = {'/fonts/misans/Regular.woff2': 'd704c1a932c0bd7e8a071d276cd81c0ed0c9fecfa26ac234f4bed0559fe1cb2d', '/fonts/misans/Bold.woff2': '1c5a7515b61bc82baaa2e2c2fdae2032479fb9a99e09d4d021dc17314fc5939b', '/fonts/noto-sans-sc/Variable.ttf': 'a3041811a78c361b1de50f953c805e0244951c21c5bd412f7232ef0d899af0da', '/fonts/lxgw-wenkai-screen/Regular.ttf': 'cd1a6fa39c4ea42fd8f4e289945789b0e510cf7016435640f8893cdad9b220f3', '/fonts/chill-round/Regular.ttf': '7ccfc2cd0e971edeac7f65516df8db379f0f84d0d8ff79ee4dfe377956418d3b', '/fonts/chill-round/Bold.ttf': 'f2c4f9295d9d04a1eb6392ae3c51ecf2a9ffab355a45c6686eeec5eede3381ec'}
