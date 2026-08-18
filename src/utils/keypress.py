"""Single-keypress terminal input, for CLIs where pressing Enter 130 times hurts.

Both the curation CLI and the calibration grading CLI want "press one key, it
advances". On a Unix terminal that means putting stdin into raw mode for the
duration of one read; there is no stdlib helper for it.
"""

from __future__ import annotations

import sys
import termios
import tty


def getch() -> str:
    """Read exactly one character from stdin without waiting for Enter.

    Falls back to line input when stdin is not a tty (piped input, tests).
    Raises KeyboardInterrupt on Ctrl-C, since raw mode disables the usual
    signal handling.
    """
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        if not line:
            raise EOFError
        return line.strip()[:1]

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        char = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    if char == "\x03":  # Ctrl-C
        raise KeyboardInterrupt
    if char == "\x04":  # Ctrl-D
        raise EOFError
    return char
