import pytest

from macrorec.events import Click, KeyTap, Move, Sleep
from macrorec.timeline import MIN_SLEEP_MS, build_schedule, to_events


def test_sleeps_become_offsets_and_disappear_from_the_steps():
    schedule = build_schedule(
        [KeyTap("a"), Sleep(250), Click("left"), Sleep(750), KeyTap("b")]
    )
    assert [(step.at, step.event) for step in schedule] == [
        (0.0, KeyTap("a")),
        (0.25, Click("left")),
        (1.0, KeyTap("b")),
    ]
    assert schedule.duration == 1.0


def test_trailing_sleep_extends_the_duration_but_adds_no_step():
    schedule = build_schedule([KeyTap("a"), Sleep(500)])
    assert len(schedule) == 1
    assert schedule.duration == 0.5


def test_speed_scales_every_delay_including_explicit_sleeps():
    events = [KeyTap("a"), Sleep(1000), KeyTap("b"), Sleep(1000), KeyTap("c")]
    fast = build_schedule(events, speed=2.0)
    slow = build_schedule(events, speed=0.5)
    assert [step.at for step in fast] == [0.0, 0.5, 1.0]
    assert [step.at for step in slow] == [0.0, 2.0, 4.0]


def test_speed_must_be_positive():
    with pytest.raises(ValueError):
        build_schedule([KeyTap("a")], speed=0)


def test_looping_does_not_accumulate_drift():
    # 100 passes of a 3s macro: the last step must land exactly, not 100 roundings late.
    schedule = build_schedule([KeyTap("a"), Sleep(1500), KeyTap("b"), Sleep(1500)])
    steps = list(schedule.iterate(loops=100))
    assert len(steps) == 200
    assert steps[-2].at == pytest.approx(99 * 3.0, abs=1e-9)
    assert steps[-1].at == pytest.approx(99 * 3.0 + 1.5, abs=1e-9)
    # Every gap between iterations is identical, which is what "drift-free" means.
    starts = [steps[i].at for i in range(0, len(steps), 2)]
    gaps = {round(b - a, 9) for a, b in zip(starts, starts[1:])}
    assert gaps == {3.0}


def test_infinite_loop_keeps_advancing():
    schedule = build_schedule([KeyTap("a"), Sleep(1000)])
    stream = schedule.iterate(loops=0)
    firsts = [next(stream).at for _ in range(5)]
    assert firsts == [0.0, 1.0, 2.0, 3.0, 4.0]


def test_to_events_inserts_sleeps_between_timestamps():
    timed = [(0.0, KeyTap("a")), (0.25, Move(3, 4)), (1.0, Click("left"))]
    assert to_events(timed) == [
        KeyTap("a"),
        Sleep(250),
        Move(3, 4),
        Sleep(750),
        Click("left"),
    ]


def test_to_events_defers_a_sub_threshold_gap_rather_than_dropping_it():
    """No `sleep 1` line, because one would be capture noise - but the millisecond is
    folded into the next sleep, not lost. 0.5s of wall clock, 500ms of sleep."""
    timed = [(0.0, KeyTap("a")), (0.001, KeyTap("b")), (0.5, KeyTap("c"))]
    assert to_events(timed) == [KeyTap("a"), KeyTap("b"), Sleep(500), KeyTap("c")]


def test_sub_threshold_gaps_accumulate_into_the_next_sleep():
    """The defect this replaced: raw XRecord motion arrives every 1-3ms, so every gap
    fell under the floor, each was dropped on its own, and a whole fast stroke replayed
    with no delay at all."""
    timed = [(i * 0.002, KeyTap("a")) for i in range(301)]  # 300 gaps of 2ms = 0.600s

    events = to_events(timed)
    total = sum(e.ms for e in events if isinstance(e, Sleep))
    assert total == pytest.approx(600, abs=MIN_SLEEP_MS)
    assert build_schedule(events).duration == pytest.approx(0.6, abs=0.005)


def test_no_drift_accumulates_over_mixed_gaps():
    """The anchor advances by the amount emitted, never to the event's own timestamp,
    so the sub-millisecond remainder is not shaved off once per sleep."""
    timed = [(0.0, KeyTap("a"))]
    at = 0.0
    for index in range(50):
        at += 0.0031 if index % 2 else 0.0234  # one over the floor, one under
        timed.append((at, KeyTap("a")))

    events = to_events(timed)
    total = sum(e.ms for e in events if isinstance(e, Sleep))
    # Error is bounded by the one residual left unemitted at the end, not by 50 of them.
    assert total == pytest.approx(at * 1000, abs=MIN_SLEEP_MS)


def test_to_events_and_build_schedule_are_inverses():
    timed = [(0.0, KeyTap("a")), (0.25, Click("left")), (1.75, KeyTap("b"))]
    schedule = build_schedule(to_events(timed))
    assert [(step.at, step.event) for step in schedule] == timed
