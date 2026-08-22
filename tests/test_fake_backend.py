import threading

import pytest

from macrorec.backend.fake import FakePlayer, FakeRecorder
from macrorec.events import (
    Click,
    KeyDown,
    KeyTap,
    KeyUp,
    MouseDown,
    MouseUp,
    Move,
    Scroll,
    Sleep,
    TypeText,
)
from macrorec.script import format_macro, parse
from macrorec.timeline import build_schedule


def test_perform_dispatches_every_event_kind():
    player = FakePlayer()
    for event in [
        KeyTap("a"), KeyDown("Control_L"), KeyUp("Control_L"),
        Move(10, 20), Click("left"), MouseDown("right"), MouseUp("right"),
        Scroll("down", 3),
    ]:
        player.perform(event)
    assert player.calls == [
        ("key_down", "a"), ("key_up", "a"),
        ("key_down", "Control_L"), ("key_up", "Control_L"),
        ("move", 10, 20),
        ("button_down", "left"), ("button_up", "left"),
        ("button_down", "right"), ("button_up", "right"),
        ("scroll", "down"), ("scroll", "down"), ("scroll", "down"),
    ]


def test_type_expands_to_keysym_names():
    player = FakePlayer()
    player.perform(TypeText("hi!"))
    assert player.calls == [
        ("key_down", "h"), ("key_up", "h"),
        ("key_down", "i"), ("key_up", "i"),
        ("key_down", "exclam"), ("key_up", "exclam"),
    ]


def test_sleep_is_the_timelines_job_not_the_players():
    with pytest.raises(TypeError, match="timeline"):
        FakePlayer().perform(Sleep(100))


def test_recorder_state_machine():
    recorder = FakeRecorder([(0.0, KeyTap("a"))])
    captured = []
    recorder.start(lambda at, event: captured.append((at, event)))
    assert recorder.drain(), "canned script was not delivered"
    assert captured == [(0.0, KeyTap("a"))]
    assert recorder.is_recording, "exhausting the script is not the same as stopping"
    with pytest.raises(RuntimeError):
        recorder.start(lambda at, event: None)
    recorder.stop()
    assert not recorder.is_recording


def test_recorder_delivers_off_the_calling_thread():
    """The X recorder blocks in record_enable_context(), so events can only arrive
    after start() returns. The fake has to behave the same way."""
    seen_on = []
    recorder = FakeRecorder([(0.0, KeyTap("a"))])
    recorder.start(lambda at, event: seen_on.append(threading.current_thread()))
    assert recorder.drain()
    recorder.stop()
    assert seen_on and seen_on[0] is not threading.current_thread()


def test_end_to_end_through_file_and_schedule():
    """Record, write, re-read, schedule, replay - all without a display."""
    recorder = FakeRecorder([
        (0.0, KeyTap("Return")),
        (0.25, Move(640, 400)),
        (0.25, Click("left")),
        (0.75, TypeText("ok")),
    ])
    captured = []
    recorder.start(lambda at, event: captured.append((at, event)))
    assert recorder.drain()
    recorder.stop()

    from macrorec.collapse import collapse_motion
    from macrorec.events import Macro
    from macrorec.timeline import to_events

    macro = Macro(events=collapse_motion(to_events(captured)), name="round-trip")
    reloaded = parse(format_macro(macro))
    assert reloaded == macro

    schedule = build_schedule(reloaded.events, speed=reloaded.speed)
    player = FakePlayer()
    for step in schedule:
        player.perform(step.event)

    assert [step.at for step in schedule] == [0.0, 0.25, 0.25, 0.75]
    assert player.calls == [
        ("key_down", "Return"), ("key_up", "Return"),
        ("move", 640, 400),
        ("button_down", "left"), ("button_up", "left"),
        ("key_down", "o"), ("key_up", "o"),
        ("key_down", "k"), ("key_up", "k"),
    ]
