"""The one shared vocabulary: what a macro is made of.

Everything else in the package speaks in these types. They carry keysym *names*
rather than keycodes so a macro file stays readable and portable; resolving a name
to a keycode is the backend's job, at replay time.
"""

from __future__ import annotations

from dataclasses import dataclass, field

BUTTONS = ("left", "middle", "right")
SCROLL_DIRECTIONS = ("up", "down", "left", "right")

#: Friendly names a human may type for keys whose real keysym is less obvious.
#: Parsing normalises through this table, so a file always round-trips as keysyms.
KEY_ALIASES = {
    "ctrl": "Control_L",
    "control": "Control_L",
    "alt": "Alt_L",
    "shift": "Shift_L",
    "super": "Super_L",
    "win": "Super_L",
    "meta": "Meta_L",
    "esc": "Escape",
    "enter": "Return",
    "del": "Delete",
    "ins": "Insert",
    "pgup": "Prior",
    "pgdn": "Next",
    "space": "space",
}

#: Buttons may be written as numbers too, the way X names them.
BUTTON_ALIASES = {"1": "left", "2": "middle", "3": "right"}


def normalise_key(name: str) -> str:
    return KEY_ALIASES.get(name.lower(), name)


def normalise_button(name: str) -> str:
    return BUTTON_ALIASES.get(name, name.lower())


@dataclass(frozen=True)
class Event:
    """Base class. Instances are immutable so a parsed macro can be shared freely."""


@dataclass(frozen=True)
class KeyTap(Event):
    """A press immediately followed by a release."""

    sym: str


@dataclass(frozen=True)
class KeyDown(Event):
    sym: str


@dataclass(frozen=True)
class KeyUp(Event):
    sym: str


@dataclass(frozen=True)
class TypeText(Event):
    """Sugar. Expands to a keysym sequence at replay time, never emitted by the
    recorder, but convenient to write by hand."""

    text: str


@dataclass(frozen=True)
class Move(Event):
    x: int
    y: int


@dataclass(frozen=True)
class MoveRel(Event):
    """A relative pointer displacement, from XI2 raw input (`backend/xi2.py`).

    Deliberately not a `Move` subclass: `collapse.collapse_motion` and
    `collapse.sample_motion` both test `isinstance(event, Move)` to find pointer
    motion, and collapsing a run of deltas to its last one would silently discard
    every delta before it - the opposite of what `collapse.accumulate_motion`
    exists to prevent. `MOUSE_EVENTS` deliberately excludes it too: that tuple
    means "an action that justifies keeping a preceding move", and `MoveRel` is
    motion, not an action.
    """

    dx: int
    dy: int


@dataclass(frozen=True)
class Click(Event):
    button: str = "left"


@dataclass(frozen=True)
class MouseDown(Event):
    button: str = "left"


@dataclass(frozen=True)
class MouseUp(Event):
    button: str = "left"


@dataclass(frozen=True)
class Scroll(Event):
    direction: str = "up"
    count: int = 1


@dataclass(frozen=True)
class Sleep(Event):
    """A delay, in milliseconds. Scaled by the speed scalar like every other delay."""

    ms: int


MOUSE_EVENTS = (Click, MouseDown, MouseUp, Scroll)


@dataclass
class Macro:
    """A parsed macro file: header fields plus the event stream."""

    events: list[Event] = field(default_factory=list)
    name: str | None = None
    version: int = 1
    layout: str | None = None
    speed: float = 1.0


#: ASCII characters whose keysym name differs from the character itself.
_PUNCTUATION_KEYSYMS = {
    " ": "space", "!": "exclam", '"': "quotedbl", "#": "numbersign",
    "$": "dollar", "%": "percent", "&": "ampersand", "'": "apostrophe",
    "(": "parenleft", ")": "parenright", "*": "asterisk", "+": "plus",
    ",": "comma", "-": "minus", ".": "period", "/": "slash",
    ":": "colon", ";": "semicolon", "<": "less", "=": "equal",
    ">": "greater", "?": "question", "@": "at", "[": "bracketleft",
    "\\": "backslash", "]": "bracketright", "^": "asciicircum",
    "_": "underscore", "`": "grave", "{": "braceleft", "|": "bar",
    "}": "braceright", "~": "asciitilde", "\n": "Return", "\t": "Tab",
}


def keysym_for_char(char: str) -> str:
    """Keysym name for a single character. Whether a modifier is needed to produce
    it depends on the keymap, so that decision belongs to the backend."""
    if char in _PUNCTUATION_KEYSYMS:
        return _PUNCTUATION_KEYSYMS[char]
    return char


def expand_type(text: str) -> list[KeyTap]:
    return [KeyTap(keysym_for_char(c)) for c in text]
