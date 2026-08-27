import pytest

from macrorec.collapse import (
    MOTION_SAMPLE_SECONDS,
    accumulate_motion,
    collapse_motion,
    merge_sleeps,
    sample_motion,
)
from macrorec.events import Click, KeyTap, MouseDown, MouseUp, Move, MoveRel, Scroll, Sleep
from macrorec.timeline import MIN_SLEEP_MS, build_schedule, to_events


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
    """Travel time lands before the surviving move and dwell time after it, which is
    where they really happened: the pointer took 100ms to get there, rested 50ms, then
    clicked. Flushing all of it to one side would either hover the target for the whole
    approach or give it no hover at all."""
    events = [Move(1, 1), Sleep(100), Move(9, 9), Sleep(50), Click("left")]
    assert collapse_motion(events) == [Sleep(100), Move(9, 9), Sleep(50), Click("left")]


def test_a_drag_keeps_its_settle_time_after_the_move():
    """The reason the dwell must not move ahead of the jump: an application needs its
    moment to process the motion before the button comes up."""
    events = [MouseDown("left"), Move(1, 1), Sleep(20), Move(9, 9), Sleep(500),
              MouseUp("left")]
    assert collapse_motion(events) == [
        MouseDown("left"), Sleep(20), Move(9, 9), Sleep(500), MouseUp("left"),
    ]


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


# sample_motion: the opt-in alternative, which keeps the path instead of the endpoints.


def test_sampling_thins_a_run_to_one_move_per_interval():
    timed = [(i * 0.004, Move(i, i)) for i in range(9)]  # 4ms apart, four per interval
    kept = [event for _, event in sample_motion(timed, interval=0.016)]
    assert kept == [Move(0, 0), Move(4, 4), Move(8, 8)]


def test_the_click_position_is_exact_not_the_last_sample():
    """A held-back move is flushed before any other event, so a click lands where it
    was really made rather than up to one interval behind."""
    timed = [
        (0.000, Move(0, 0)),
        (0.004, Move(1, 1)),
        (0.008, Move(2, 2)),
        (0.010, MouseDown("left")),
    ]
    assert sample_motion(timed, interval=0.016) == [
        (0.000, Move(0, 0)), (0.008, Move(2, 2)), (0.010, MouseDown("left")),
    ]


def test_non_motion_events_pass_through_untouched():
    timed = [(0.0, KeyTap("a")), (0.1, Sleep(50)), (0.2, Scroll("down", 2))]
    assert sample_motion(timed) == timed


def test_the_resting_position_survives_with_nothing_after_it():
    """Unlike collapse_motion, a trailing move is the end of the stroke, not noise."""
    timed = [(0.0, Move(0, 0)), (0.002, Move(1, 1)), (0.004, Move(9, 9))]
    assert sample_motion(timed, interval=0.016)[-1] == (0.004, Move(9, 9))


def test_sampling_empty_and_single_inputs():
    assert sample_motion([]) == []
    assert sample_motion([(0.0, Move(3, 4))]) == [(0.0, Move(3, 4))]
    assert sample_motion([(0.0, KeyTap("a"))]) == [(0.0, KeyTap("a"))]


def test_the_default_interval_clears_the_min_sleep_floor():
    """The two constants are one knob seen twice. If the sample interval ever drops
    to or below MIN_SLEEP_MS, to_events discards the gaps and playback warps."""
    assert MOTION_SAMPLE_SECONDS * 1000 > MIN_SLEEP_MS


def test_a_sampled_path_replays_spread_over_time_not_all_at_once():
    """The load-bearing one. Sampling is what keeps a path to a readable number of
    lines; `to_events` keeps its duration either way, so what separates the two is
    volume, not timing."""
    timed = [(i * 0.002, Move(i, i)) for i in range(100)]

    raw = to_events(timed)
    raw_moves = [e for e in raw if isinstance(e, Move)]
    assert len(raw_moves) == 100, "unsampled motion really is one line per sample"
    raw_offsets = [step.at for step in build_schedule(raw)]
    assert raw_offsets[-1] == pytest.approx(0.198, abs=0.01), (
        "and it keeps its duration: sub-floor gaps are deferred, not dropped"
    )

    events = to_events(sample_motion(timed))
    offsets = [step.at for step in build_schedule(events)]
    assert offsets == sorted(offsets)
    assert len(set(offsets)) > 1
    # ~0.2s of motion at one sample per 16ms, down from 100 raw events.
    assert 8 <= len(offsets) <= 16
    assert offsets[-1] == pytest.approx(0.192, abs=0.02)


def test_sampling_is_not_collapsing():
    """The two reductions disagree on purpose: one keeps the route, one the endpoint."""
    timed = [(i * 0.02, Move(i, i)) for i in range(5)] + [(0.1, Click("left"))]
    sampled = [event for _, event in sample_motion(timed)]
    assert sampled.count(Move(1, 1)) == 1

    collapsed = [e for e in collapse_motion(to_events(timed))
                 if not isinstance(e, Sleep)]
    assert collapsed == [Move(4, 4), Click("left")]


def test_moverel_is_not_a_move_subclass():
    """Load-bearing for `collapse_motion` and `sample_motion`: both find pointer
    motion with `isinstance(event, Move)`, and a `MoveRel` that matched would get
    collapsed to its last delta, silently discarding every one before it."""
    assert not isinstance(MoveRel(1, 1), Move)


# merge_sleeps: the run of `sleep 5` lines a recovered stroke leaves behind.


def test_adjacent_sleeps_become_one():
    events = [Move(1, 1), Sleep(5), Sleep(5), Sleep(6), Click("left")]
    assert merge_sleeps(events) == [Move(1, 1), Sleep(16), Click("left")]


def test_merging_handles_leading_and_trailing_runs():
    assert merge_sleeps([Sleep(5), Sleep(5), KeyTap("a"), Sleep(7), Sleep(3)]) == [
        Sleep(10), KeyTap("a"), Sleep(10),
    ]


def test_merging_leaves_separated_sleeps_alone():
    events = [KeyTap("a"), Sleep(100), KeyTap("b"), Sleep(50), KeyTap("c")]
    assert merge_sleeps(events) == events


def test_merging_is_idempotent_and_handles_empty():
    events = [Sleep(5), Sleep(5), Click("left")]
    once = merge_sleeps(events)
    assert merge_sleeps(once) == once
    assert merge_sleeps([]) == []
    assert merge_sleeps([Click("left")]) == [Click("left")]


def test_fast_motion_keeps_its_elapsed_time_through_the_default_path():
    """The regression guard for the whole chain, not just one function. 0.6s of raw
    motion at XRecord's real 2ms delivery rate used to reach `build_schedule` with no
    sleeps at all, so the click fired at offset 0.000 instead of 0.6s in."""
    timed = [(i * 0.002, Move(i, i)) for i in range(300)]
    timed.append((0.600, Click("left")))

    events = merge_sleeps(collapse_motion(to_events(timed)))
    schedule = build_schedule(events)

    assert schedule.duration == pytest.approx(0.6, abs=MIN_SLEEP_MS / 1000.0)
    click = [step for step in schedule if isinstance(step.event, Click)][0]
    assert click.at == pytest.approx(0.6, abs=MIN_SLEEP_MS / 1000.0)
    # The point of the default mode survives: one move, and 0.6s written as two lines
    # rather than 120. Two, not one, because the split is the whole design - the travel
    # goes ahead of the jump and the rest on the target after it.
    assert events == [Sleep(594), Move(299, 299), Sleep(6), Click("left")]


def test_collapse_motion_leaves_moverel_untouched():
    """`collapse_motion` only recognises `Move`, so a `MoveRel` run passes straight
    through instead of being reduced to its last delta - reducing deltas to one is
    meaningless, per `collapse.accumulate_motion`'s docstring."""
    events = [MoveRel(1, 0), MoveRel(2, 0), MoveRel(3, 0), Click("left")]
    assert collapse_motion(events) == events


# accumulate_motion: the MoveRel analogue of sample_motion, which sums instead of
# keeping the last delta.


def test_accumulating_sums_deltas_within_one_interval():
    timed = [(i * 0.004, MoveRel(1, 2)) for i in range(4)]  # one interval, 16ms
    out = accumulate_motion(timed, interval=0.016)
    assert [event for _, event in out] == [MoveRel(4, 8)]


def test_accumulating_spans_several_windows_and_preserves_the_total():
    """The hole a same-window test would hide: summing three windows of deltas
    that individually net to zero would still pass a buggy keep-last
    implementation if the test only checked one window's sum."""
    deltas = [(1, -1), (2, -2), (3, -3), (4, -4), (5, -5), (6, -6), (7, -7)]
    timed = [(i * 0.006, MoveRel(dx, dy)) for i, (dx, dy) in enumerate(deltas)]

    out = accumulate_motion(timed, interval=0.016)
    windows = [event for _, event in out]
    assert len(windows) >= 3, "the seven deltas at 6ms apart must span several windows"

    want_dx = sum(dx for dx, _ in deltas)
    want_dy = sum(dy for _, dy in deltas)
    assert sum(e.dx for e in windows) == want_dx
    assert sum(e.dy for e in windows) == want_dy


def test_keep_last_would_lose_the_turn_that_summing_preserves():
    """The regression `accumulate_motion` exists to prevent: swap in keep-last
    (what `sample_motion` does) over the same input and the total goes missing."""
    timed = [(i * 0.002, MoveRel(1, 1)) for i in range(8)]  # 16ms of 2ms deltas
    summed = accumulate_motion(timed, interval=0.016)
    assert sum(e.dx for _, e in summed) == 8

    kept_last = [(at, event) for at, event in timed if at == timed[-1][0]]
    assert sum(e.dx for _, e in kept_last) == 1, (
        "keep-last recovers one sample's worth, not the turn")


def test_accumulating_flushes_before_a_non_motion_event():
    """Same rule as `sample_motion`: a held-back sum is flushed before any other
    event, so a click's preceding displacement is exact rather than up to one
    interval stale."""
    timed = [
        (0.000, MoveRel(1, 0)),
        (0.004, MoveRel(1, 0)),
        (0.008, MoveRel(1, 0)),
        (0.010, MouseDown("left")),
    ]
    assert accumulate_motion(timed, interval=0.016) == [
        (0.008, MoveRel(3, 0)), (0.010, MouseDown("left")),
    ]


def test_accumulating_non_motion_events_pass_through_untouched():
    timed = [(0.0, KeyTap("a")), (0.1, Sleep(50)), (0.2, Scroll("down", 2))]
    assert accumulate_motion(timed) == timed


def test_accumulating_a_trailing_sum_survives_with_nothing_after_it():
    timed = [(0.0, MoveRel(1, 1)), (0.002, MoveRel(2, 2)), (0.004, MoveRel(3, 3))]
    assert accumulate_motion(timed, interval=0.016) == [(0.004, MoveRel(6, 6))]


def test_accumulating_empty_and_single_inputs():
    assert accumulate_motion([]) == []
    assert accumulate_motion([(0.0, MoveRel(3, 4))]) == [(0.0, MoveRel(3, 4))]
    assert accumulate_motion([(0.0, KeyTap("a"))]) == [(0.0, KeyTap("a"))]
