"""English Braille (Grade-1) encoder used by the tactile-map renderer.

Two outputs per string:
  - a Unicode string in the U+2800 block (``⠁⠃⠉…``) useful for previews,
    text companions, copy/paste.
  - a list of dot-bitmasks (0..63) that the renderer turns into
    correctly-sized raised dots for swell-paper / embosser output.

A braille cell has six positions, numbered:

    1 · · 4
    2 · · 5
    3 · · 6

In Unicode (U+2800 base) dot *n* is bit *n-1* so the mask composes
directly as ``chr(0x2800 + mask)``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

# ── Grade-1 English Braille lookup ───────────────────────────────────────────
# (letters → dot bitmask)
_LETTERS: dict[str, int] = {
    "a": 0b000001,
    "b": 0b000011,
    "c": 0b001001,
    "d": 0b011001,
    "e": 0b010001,
    "f": 0b001011,
    "g": 0b011011,
    "h": 0b010011,
    "i": 0b001010,
    "j": 0b011010,
    "k": 0b000101,
    "l": 0b000111,
    "m": 0b001101,
    "n": 0b011101,
    "o": 0b010101,
    "p": 0b001111,
    "q": 0b011111,
    "r": 0b010111,
    "s": 0b001110,
    "t": 0b011110,
    "u": 0b100101,
    "v": 0b100111,
    "w": 0b111010,
    "x": 0b101101,
    "y": 0b111101,
    "z": 0b110101,
}

# Digits 1..9, 0 reuse the a..j masks preceded by the number indicator (⠼).
_DIGITS: dict[str, int] = {
    "1": _LETTERS["a"],
    "2": _LETTERS["b"],
    "3": _LETTERS["c"],
    "4": _LETTERS["d"],
    "5": _LETTERS["e"],
    "6": _LETTERS["f"],
    "7": _LETTERS["g"],
    "8": _LETTERS["h"],
    "9": _LETTERS["i"],
    "0": _LETTERS["j"],
}

# Indicators + common punctuation.
CAP_INDICATOR = 0b100000              # dot 6 alone  (⠠)
NUMBER_INDICATOR = 0b111100           # dots 3,4,5,6 (⠼)
LETTER_INDICATOR = 0b110000           # dots 5,6     (⠰) — reset out of number mode
SPACE = 0b000000                      # empty cell   (⠀)

_PUNCT: dict[str, int] = {
    " ": SPACE,
    ",": 0b000010,                    # dot 2      ⠂
    ";": 0b000110,                    # dots 2,3   ⠆
    ":": 0b010010,                    # dots 2,5   ⠒
    ".": 0b110010,                    # dots 2,5,6 ⠲
    "?": 0b010110,                    # dots 2,3,6 ⠦ (Louis Braille variant)
    "!": 0b010110,                    # share ?
    "-": 0b100100,                    # dots 3,6   ⠤
    "/": 0b001100,                    # dots 3,4   ⠌
    "&": 0b101111,                    # dots 1,2,3,4,6 (approx)
    "#": NUMBER_INDICATOR,
    "(": 0b111011,                    # dots 2,3,5,6 ⠶ (unified bracket)
    ")": 0b111011,
}


@dataclass(frozen=True)
class BrailleCell:
    mask: int            # 0..63 — which dots are raised
    char: str            # Unicode braille char (U+2800 + mask)


def _cell(mask: int) -> BrailleCell:
    return BrailleCell(mask=mask, char=chr(0x2800 + (mask & 0x3F)))


def text_to_cells(s: str) -> list[BrailleCell]:
    """Encode an ASCII-ish string as a list of :class:`BrailleCell`.

    Rules:
      * Uppercase letter → capital indicator + letter.
      * Runs of 2+ uppercase letters → double capital indicator + letters
        (the double-cap applies to the whole word, per standard English
        Braille).
      * Digits → number indicator + digit-as-letter; terminated by the
        first non-digit (the letter indicator is inserted if a letter
        a..j immediately follows so it isn't re-read as a digit).
      * Unknown characters fall through as a blank space cell.
    """
    cells: list[BrailleCell] = []
    i = 0
    n = len(s)
    in_number = False

    def run_is_all_upper(start: int) -> int:
        """Return length of the contiguous run of upper-case letters starting
        at *start* (≥ 1 implies start char was upper)."""
        j = start
        while j < n and s[j].isalpha() and s[j].isupper():
            j += 1
        return j - start

    while i < n:
        ch = s[i]

        if ch.isalpha():
            if ch.isupper():
                run = run_is_all_upper(i)
                if run >= 2:
                    # Double-cap indicator applies to the word.
                    cells.append(_cell(CAP_INDICATOR))
                    cells.append(_cell(CAP_INDICATOR))
                    for k in range(run):
                        cells.append(_cell(_LETTERS[s[i + k].lower()]))
                    i += run
                    in_number = False
                    continue
                cells.append(_cell(CAP_INDICATOR))
                cells.append(_cell(_LETTERS[ch.lower()]))
            else:
                if in_number and ch in "abcdefghij":
                    # Disambiguate: a following letter after a digit run
                    # is otherwise re-read as another digit.
                    cells.append(_cell(LETTER_INDICATOR))
                cells.append(_cell(_LETTERS[ch]))
            in_number = False
            i += 1
            continue

        if ch.isdigit():
            if not in_number:
                cells.append(_cell(NUMBER_INDICATOR))
                in_number = True
            cells.append(_cell(_DIGITS[ch]))
            i += 1
            continue

        # Punctuation / whitespace / unknown.
        cells.append(_cell(_PUNCT.get(ch, SPACE)))
        in_number = False
        i += 1

    return cells


def text_to_braille(s: str) -> str:
    """Return the Unicode Braille string for *s* (no dot rendering)."""
    return "".join(c.char for c in text_to_cells(s))


def cells_to_masks(cells: Iterable[BrailleCell]) -> list[int]:
    return [c.mask for c in cells]


# ── Dot geometry helpers ─────────────────────────────────────────────────────
#
# Real embossed Braille is standardised (Library of Congress / ADA):
#   dot diameter .057–.063 in ≈ 1.45 mm
#   dot spacing within cell 0.092 in ≈ 2.34 mm
#   cell-to-cell spacing 0.242 in ≈ 6.15 mm
#   line-to-line spacing 0.400 in ≈ 10.16 mm
#
# We expose these as millimetre constants so the renderer can scale them
# against the target output DPI without hard-coding pixel values here.

DOT_DIAMETER_MM = 1.5
INTRA_CELL_SPACING_MM = 2.5            # between dots within one cell
INTER_CELL_SPACING_MM = 6.2            # centre-to-centre, cell↔cell (horizontal)
INTER_LINE_SPACING_MM = 10.0           # centre-to-centre, line↔line (vertical)


def cell_dot_offsets_mm() -> list[tuple[int, tuple[float, float]]]:
    """Return the ``(dot_number, (dx_mm, dy_mm))`` list for a single cell,
    origin = top-left dot (dot 1)."""
    s = INTRA_CELL_SPACING_MM
    return [
        (1, (0.0, 0.0)),
        (2, (0.0, s)),
        (3, (0.0, 2 * s)),
        (4, (s,   0.0)),
        (5, (s,   s)),
        (6, (s,   2 * s)),
    ]


def active_dots(mask: int) -> list[int]:
    """Return the list of dot numbers (1..6) that are raised in *mask*."""
    return [d for d in range(1, 7) if (mask >> (d - 1)) & 1]
