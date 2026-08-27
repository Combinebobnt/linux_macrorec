"""The macro DSL: parser and formatter. Pure, no X dependency.

One command per line. A `#` starts a comment and runs to the end of the line, unless
it falls inside a quoted string. Comments other than the leading `# macro: <name>`
are dropped on parse, so a formatted file is canonical rather than byte-identical to
whatever a human wrote.
"""

from __future__ import annotations

import re

from .events import (
    BUTTONS,
    SCROLL_DIRECTIONS,
    Click,
    Event,
    KeyDown,
    KeyTap,
    KeyUp,
    Macro,
    MouseDown,
    MouseUp,
    Move,
    MoveRel,
    Scroll,
    Sleep,
    TypeText,
    normalise_button,
    normalise_key,
)

SUPPORTED_VERSION = 1

#: Every command the language has. Load-bearing: `_parse_command` rejects anything
#: not listed here, and the README test iterates it, so adding a command without
#: documenting it fails the suite.
COMMANDS = (
    "key", "keydown", "keyup", "type", "move", "moverel",
    "click", "mousedown", "mouseup", "scroll", "sleep",
)

#: Header directives, all of which must precede the first command.
DIRECTIVES = ("version", "layout", "speed")

_NAME_COMMENT = re.compile(r"^#\s*macro:\s*(.+?)\s*$")
_DURATION = re.compile(r"^(\d+)(ms|s)$")
_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}


class ScriptError(ValueError):
    """A malformed macro file. Carries the line number so the GUI can point at it."""

    def __init__(self, line_no: int, message: str):
        super().__init__(f"line {line_no}: {message}")
        self.line_no = line_no
        self.message = message


def parse(text: str) -> Macro:
    macro = Macro()
    seen_command = False

    for line_no, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            match = _NAME_COMMENT.match(line)
            if match and macro.name is None and not seen_command:
                macro.name = match.group(1)
            continue

        line = _strip_comment(line)
        if not line:
            continue

        word, _, rest = line.partition(" ")
        word = word.lower()
        rest = rest.strip()

        if word in DIRECTIVES:
            if seen_command:
                raise ScriptError(line_no, f"'{word}' must appear before any command")
            _parse_header(macro, word, rest, line_no)
            continue

        seen_command = True
        macro.events.append(_parse_command(word, rest, line_no))

    return macro


def _strip_comment(line: str) -> str:
    """Cut a trailing `# ...` comment, leaving a `#` that sits inside a string."""
    in_string = False
    index = 0
    while index < len(line):
        char = line[index]
        if in_string:
            if char == "\\":
                index += 2
                continue
            if char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "#":
            return line[:index].rstrip()
        index += 1
    return line


def _parse_header(macro: Macro, word: str, rest: str, line_no: int) -> None:
    if not rest:
        raise ScriptError(line_no, f"'{word}' needs a value")
    if word == "version":
        try:
            macro.version = int(rest)
        except ValueError:
            raise ScriptError(line_no, f"version must be a number, got {rest!r}") from None
        if macro.version > SUPPORTED_VERSION:
            raise ScriptError(
                line_no,
                f"file is version {macro.version}, this build understands "
                f"up to {SUPPORTED_VERSION}",
            )
    elif word == "layout":
        macro.layout = rest
    else:
        try:
            macro.speed = float(rest)
        except ValueError:
            raise ScriptError(line_no, f"speed must be a number, got {rest!r}") from None
        if macro.speed <= 0:
            raise ScriptError(line_no, "speed must be greater than zero")


def _parse_command(word: str, rest: str, line_no: int) -> Event:
    if word not in COMMANDS:
        raise ScriptError(line_no, f"unknown command {word!r}")

    if word in ("key", "keydown", "keyup"):
        if not rest or " " in rest:
            raise ScriptError(line_no, f"'{word}' takes exactly one key name")
        sym = normalise_key(rest)
        return {"key": KeyTap, "keydown": KeyDown, "keyup": KeyUp}[word](sym)

    if word == "type":
        return TypeText(_parse_string(rest, line_no))

    if word == "move":
        parts = rest.split()
        if len(parts) != 2:
            raise ScriptError(line_no, "'move' takes an x and a y")
        try:
            return Move(int(parts[0]), int(parts[1]))
        except ValueError:
            raise ScriptError(line_no, f"'move' needs integer coordinates, got {rest!r}") from None

    if word == "moverel":
        parts = rest.split()
        if len(parts) != 2:
            raise ScriptError(line_no, "'moverel' takes a dx and a dy")
        try:
            return MoveRel(int(parts[0]), int(parts[1]))
        except ValueError:
            raise ScriptError(line_no, f"'moverel' needs integer deltas, got {rest!r}") from None

    if word in ("click", "mousedown", "mouseup"):
        button = normalise_button(rest) if rest else "left"
        if button not in BUTTONS:
            raise ScriptError(line_no, f"unknown button {rest!r}, expected one of {', '.join(BUTTONS)}")
        return {"click": Click, "mousedown": MouseDown, "mouseup": MouseUp}[word](button)

    if word == "scroll":
        parts = rest.split()
        if not parts:
            raise ScriptError(line_no, "'scroll' needs a direction")
        direction = parts[0].lower()
        if direction not in SCROLL_DIRECTIONS:
            raise ScriptError(
                line_no,
                f"unknown scroll direction {parts[0]!r}, expected one of "
                f"{', '.join(SCROLL_DIRECTIONS)}",
            )
        count = 1
        if len(parts) == 2:
            try:
                count = int(parts[1])
            except ValueError:
                raise ScriptError(line_no, f"scroll count must be a number, got {parts[1]!r}") from None
            if count < 1:
                raise ScriptError(line_no, "scroll count must be at least 1")
        elif len(parts) > 2:
            raise ScriptError(line_no, "'scroll' takes a direction and an optional count")
        return Scroll(direction, count)

    if word == "sleep":
        match = _DURATION.match(rest)
        if not match:
            raise ScriptError(line_no, f"'sleep' needs a duration like 250ms or 2s, got {rest!r}")
        value = int(match.group(1))
        return Sleep(value * 1000 if match.group(2) == "s" else value)

    raise ScriptError(line_no, f"unknown command {word!r}")


def _parse_string(rest: str, line_no: int) -> str:
    if len(rest) < 2 or not rest.startswith('"') or not rest.endswith('"'):
        raise ScriptError(line_no, "'type' needs a double-quoted string")
    body = rest[1:-1]
    out = []
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\":
            if index + 1 >= len(body):
                raise ScriptError(line_no, "string ends with a dangling backslash")
            escape = body[index + 1]
            if escape not in _ESCAPES:
                raise ScriptError(line_no, f"unknown escape '\\{escape}'")
            out.append(_ESCAPES[escape])
            index += 2
            continue
        if char == '"':
            raise ScriptError(line_no, "unescaped quote inside string")
        out.append(char)
        index += 1
    return "".join(out)


def format_macro(macro: Macro) -> str:
    lines = []
    if macro.name:
        lines.append(f"# macro: {macro.name}")
    lines.append(f"version {macro.version}")
    if macro.layout:
        lines.append(f"layout {macro.layout}")
    if macro.speed != 1.0:
        lines.append(f"speed {macro.speed:g}")
    lines.append("")
    lines.extend(format_event(event) for event in macro.events)
    return "\n".join(lines) + "\n"


def format_event(event: Event) -> str:
    if isinstance(event, KeyTap):
        return f"key {event.sym}"
    if isinstance(event, KeyDown):
        return f"keydown {event.sym}"
    if isinstance(event, KeyUp):
        return f"keyup {event.sym}"
    if isinstance(event, TypeText):
        return f'type "{_escape_string(event.text)}"'
    if isinstance(event, Move):
        return f"move {event.x} {event.y}"
    if isinstance(event, MoveRel):
        return f"moverel {event.dx} {event.dy}"
    if isinstance(event, Click):
        return f"click {event.button}"
    if isinstance(event, MouseDown):
        return f"mousedown {event.button}"
    if isinstance(event, MouseUp):
        return f"mouseup {event.button}"
    if isinstance(event, Scroll):
        return f"scroll {event.direction} {event.count}"
    if isinstance(event, Sleep):
        if event.ms and event.ms % 1000 == 0:
            return f"sleep {event.ms // 1000}s"
        return f"sleep {event.ms}ms"
    raise TypeError(f"no formatting for {type(event).__name__}")


def _escape_string(text: str) -> str:
    reverse = {value: key for key, value in _ESCAPES.items()}
    return "".join(f"\\{reverse[c]}" if c in reverse else c for c in text)
