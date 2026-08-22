from macrorec.collapse import collapse_motion
from macrorec.events import Click, KeyTap, MouseDown, MouseUp, Move, Scroll, Sleep


def test_a_run_of_moves_collapses_to_the_click_position():
    events = [Move(1, 1), Move(5, 5), Move(9, 9), Click("left")]
    assert collapse_motion(events) == [Move(9, 9), Click("left")]


def test_a_drag_keeps_both_endpoints():
    events = [
        Move(0, 0), Move(5, 5), MouseDown("left"),
        Move(6, 6), Move(20, 20), Move(40, 40), MouseUp("left"),
    ]
    assert collapse_motion(events) == [
        Move(5, 5), MouseDown("left"), Move(40, 40), MouseUp("left"),
    ]


def test_moves_leading_nowhere_are_dropped():
    events = [Move(1, 1), Move(2, 2), KeyTap("a")]
    assert collapse_motion(events) == [KeyTap("a")]


def test_trailing_moves_are_dropped():
    assert collapse_motion([Click("left"), Move(1, 1), Move(2, 2)]) == [Click("left")]


def test_scroll_counts_as_a_mouse_action():
    events = [Move(1, 1), Move(7, 7), Scroll("down", 2)]
    assert collapse_motion(events) == [Move(7, 7), Scroll("down", 2)]


def test_sleeps_between_moves_are_preserved():
    events = [Move(1, 1), Sleep(100), Move(9, 9), Sleep(50), Click("left")]
    assert collapse_motion(events) == [Move(9, 9), Sleep(100), Sleep(50), Click("left")]


def test_sleeps_survive_when_the_moves_are_dropped():
    events = [KeyTap("a"), Move(1, 1), Sleep(100), Move(2, 2), KeyTap("b")]
    assert collapse_motion(events) == [KeyTap("a"), Sleep(100), KeyTap("b")]


def test_sleeps_outside_a_motion_run_are_untouched():
    events = [KeyTap("a"), Sleep(100), KeyTap("b")]
    assert collapse_motion(events) == events


def test_a_realistic_capture_shrinks_to_a_handful_of_lines():
    raw = []
    for x in range(0, 800, 4):
        raw.append(Move(x, x // 2))
    raw.append(Click("left"))
    for x in range(800, 0, -4):
        raw.append(Move(x, x // 2))
    raw.append(Click("right"))
    collapsed = collapse_motion(raw)
    assert collapsed == [
        Move(796, 398), Click("left"), Move(4, 2), Click("right"),
    ]
    assert len(raw) > 100 and len(collapsed) == 4


def test_empty_input():
    assert collapse_motion([]) == []
