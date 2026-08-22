"""Guards against the README drifting away from the code it documents."""

from __future__ import annotations

import os
import re

import pytest

from macrorec.events import BUTTONS, KEY_ALIASES, SCROLL_DIRECTIONS
from macrorec.script import COMMANDS, DIRECTIVES, format_macro, parse

README = os.path.join(os.path.dirname(__file__), os.pardir, "README.md")


def readme() -> str:
    with open(README, encoding="utf-8") as handle:
        return handle.read()


def fenced_blocks(text: str) -> list[str]:
    return re.findall(r"```\n(.*?)```", text, re.DOTALL)


def test_the_example_macro_parses():
    blocks = [b for b in fenced_blocks(readme()) if b.startswith("# macro:")]
    assert blocks, "the documented example macro block is missing"

    macro = parse(blocks[0])
    assert macro.name == "login-sequence"
    assert macro.layout == "us"
    assert len(macro.events) == 11
    assert parse(format_macro(macro)) == macro


SAMPLES = {
    "key": "key Return",
    "keydown": "keydown ctrl",
    "keyup": "keyup ctrl",
    "type": 'type "hello"',
    "move": "move 10 20",
    "click": "click left",
    "mousedown": "mousedown left",
    "mouseup": "mouseup left",
    "scroll": "scroll up 3",
    "sleep": "sleep 250ms",
}


@pytest.mark.parametrize("command", COMMANDS)
def test_every_implemented_command_is_documented(command):
    """Driven from `script.COMMANDS`, which the parser itself uses, so adding a
    command without a README row fails here rather than drifting quietly."""
    text = readme()
    assert f"`{command} " in text or f"`{command}<" in text, (
        f"{command} is implemented but has no README row")


@pytest.mark.parametrize("directive", DIRECTIVES)
def test_every_header_directive_is_documented(directive):
    assert f"`{directive} " in text_of_readme(), (
        f"the {directive} directive is implemented but undocumented")


def text_of_readme() -> str:
    return readme()


def test_the_sample_for_every_command_parses():
    assert set(SAMPLES) == set(COMMANDS), (
        "SAMPLES must cover every command in script.COMMANDS")
    for command, sample in SAMPLES.items():
        assert parse(sample + "\n").events, f"{sample!r} does not parse"


def test_documented_aliases_all_exist():
    text = readme()
    documented = re.findall(r"`(ctrl|alt|shift|super|esc|enter|del|ins|pgup|pgdn)`",
                            text)
    assert documented, "the alias list is missing"
    for alias in set(documented):
        assert alias in KEY_ALIASES, f"README documents {alias!r}, code does not"


def test_documented_buttons_and_scroll_directions_match_the_code():
    text = readme()
    for button in BUTTONS:
        assert button in text, f"button {button!r} is undocumented"
    for direction in SCROLL_DIRECTIONS:
        assert direction in text, f"scroll direction {direction!r} is undocumented"


@pytest.mark.parametrize("heading", [
    "## Install", "## Quick usage", "## Macro file format", "## Development",
])
def test_required_sections_are_present(heading):
    assert heading in readme()


def test_install_comes_before_usage():
    """Description, then install, then quick usage, then everything else."""
    text = readme()
    assert text.index("## Install") < text.index("## Quick usage")
    assert text.index("## Quick usage") < text.index("## Macro file format")
