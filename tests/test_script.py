import pytest

from macrorec.events import (
    Click,
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
)
from macrorec.script import ScriptError, format_macro, parse

SAMPLE = """\
# macro: login-sequence
version 1
layout us

key Return
sleep 250ms
type "hello world"
sleep 1s
move 640 400
click left
sleep 500ms
keydown ctrl
key s
keyup ctrl
scroll up 3
"""


def test_parses_the_documented_sample():
    macro = parse(SAMPLE)
    assert macro.name == "login-sequence"
    assert macro.version == 1
    assert macro.layout == "us"
    assert macro.speed == 1.0
    assert macro.events == [
        KeyTap("Return"),
        Sleep(250),
        TypeText("hello world"),
        Sleep(1000),
        Move(640, 400),
        Click("left"),
        Sleep(500),
        KeyDown("Control_L"),
        KeyTap("s"),
        KeyUp("Control_L"),
        Scroll("up", 3),
    ]


def test_round_trips_through_the_formatter():
    macro = parse(SAMPLE)
    assert parse(format_macro(macro)) == macro


def test_formatting_is_stable_on_a_second_pass():
    once = format_macro(parse(SAMPLE))
    assert format_macro(parse(once)) == once


def test_key_aliases_normalise_to_keysyms():
    macro = parse("keydown ctrl\nkeydown Control_L\nkeyup ALT\nkey esc\n")
    assert macro.events == [
        KeyDown("Control_L"),
        KeyDown("Control_L"),
        KeyUp("Alt_L"),
        KeyTap("Escape"),
    ]


def test_button_numbers_and_defaults():
    macro = parse("click\nclick 2\nmousedown 3\nmouseup RIGHT\n")
    assert macro.events == [
        Click("left"),
        Click("middle"),
        MouseDown("right"),
        MouseUp("right"),
    ]


def test_sleep_units_and_formatting():
    macro = parse("sleep 250ms\nsleep 2s\nsleep 1500ms\nsleep 0ms\n")
    assert macro.events == [Sleep(250), Sleep(2000), Sleep(1500), Sleep(0)]
    body = format_macro(macro)
    assert "sleep 250ms" in body
    assert "sleep 2s" in body
    assert "sleep 1500ms" in body
    assert "sleep 0ms" in body


def test_scroll_count_defaults_to_one():
    assert parse("scroll down\n").events == [Scroll("down", 1)]


def test_comments_and_blank_lines_are_ignored():
    macro = parse("# a note\n\n   \nkey a  \n# trailing note\n")
    assert macro.events == [KeyTap("a")]
    assert macro.name is None


def test_trailing_comments_on_a_command_line():
    macro = parse(
        "version 1  # header comment\n"
        "click left  # confirm the dialog\n"
        "sleep 250ms# no space needed\n"
        "key a\t# after a tab\n"
        "   # a whole-line comment that is indented\n"
    )
    assert macro.version == 1
    assert macro.events == [Click("left"), Sleep(250), KeyTap("a")]


def test_a_hash_inside_a_string_is_not_a_comment():
    macro = parse('type "issue #42 filed"  # but this is\n')
    assert macro.events == [TypeText("issue #42 filed")]


def test_an_escaped_quote_does_not_end_the_string_for_comment_stripping():
    macro = parse(r'type "say \"hi#\" now"  # comment')
    assert macro.events == [TypeText('say "hi#" now')]


def test_speed_header():
    assert parse("speed 2.5\nkey a\n").speed == 2.5
    assert "speed 2.5" in format_macro(parse("speed 2.5\nkey a\n"))


def test_string_escapes_round_trip():
    macro = parse(r'type "a\"b\\c\nd\te"')
    assert macro.events == [TypeText('a"b\\c\nd\te')]
    assert parse(format_macro(macro)) == macro


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("wiggle 3\n", "unknown command"),
        ("key\n", "exactly one key name"),
        ("key a b\n", "exactly one key name"),
        ("move 1\n", "an x and a y"),
        ("move a b\n", "integer coordinates"),
        ("moverel 1\n", "a dx and a dy"),
        ("moverel a b\n", "integer deltas"),
        ("click sideways\n", "unknown button"),
        ("scroll\n", "needs a direction"),
        ("scroll sideways\n", "unknown scroll direction"),
        ("scroll up two\n", "scroll count must be a number"),
        ("scroll up 0\n", "at least 1"),
        ("scroll up 1 2\n", "direction and an optional count"),
        ("sleep\n", "duration like"),
        ("sleep 5\n", "duration like"),
        ("sleep -5ms\n", "duration like"),
        ("type hello\n", "double-quoted"),
        (r'type "bad\q"', "unknown escape"),
        ('type "trailing\\"', "dangling backslash"),
        ("version two\n", "version must be a number"),
        ("version 99\n", "understands"),
        ("speed fast\n", "speed must be a number"),
        ("speed 0\n", "greater than zero"),
        ("key a\nversion 1\n", "before any command"),
        ("layout\n", "needs a value"),
    ],
)
def test_malformed_input_reports_the_line(text, fragment):
    with pytest.raises(ScriptError) as caught:
        parse(text)
    assert fragment in caught.value.message
    assert caught.value.line_no >= 1


def test_error_message_carries_the_right_line_number():
    with pytest.raises(ScriptError) as caught:
        parse("# note\nkey a\nwiggle\n")
    assert caught.value.line_no == 3


def test_empty_macro_formats_and_reparses():
    assert parse(format_macro(Macro())) == Macro()


def test_moverel_parses_signed_deltas():
    macro = parse("moverel -10 20\n")
    assert macro.events == [MoveRel(-10, 20)]


@pytest.mark.parametrize("dx, dy", [(-10, 20), (0, 0), (-1, -1), (5000, -5000)])
def test_moverel_round_trips_negative_deltas(dx, dy):
    macro = Macro(events=[MoveRel(dx, dy)])
    assert parse(format_macro(macro)) == macro
