import threading
import time

import pytest

from macrorec.backend.fake import FakePlayer
from macrorec.events import KeyTap, Sleep
from macrorec.playback import Playback
from macrorec.timeline import build_schedule


class SlowPlayer(FakePlayer):
    """Injects slowly, and writes down when each call actually happened."""

    def __init__(self, cost=0.05):
        super().__init__()
        self.cost = cost
        self.origin = time.monotonic()
        self.times = []

    def key_down(self, sym):
        self.times.append(time.monotonic() - self.origin)
        time.sleep(self.cost)
        super().key_down(sym)


def test_runs_every_step_in_order():
    player = FakePlayer()
    schedule = build_schedule([KeyTap("a"), Sleep(10), KeyTap("b")])
    playback = Playback(player, schedule)
    playback.start()
    assert playback.wait(3.0)

    assert player.calls == [
        ("key_down", "a"), ("key_up", "a"),
        ("key_down", "b"), ("key_up", "b"),
    ]
    assert not playback.was_stopped


def test_a_slow_player_does_not_push_later_steps_late():
    """The point of scheduling against an origin. Each injection costs 50ms while
    the steps are 100ms apart; sleeping per-delta would land step 3 at 300ms."""
    player = SlowPlayer(cost=0.05)
    events = [KeyTap("a"), Sleep(100), KeyTap("b"), Sleep(100), KeyTap("c")]
    playback = Playback(player, build_schedule(events))
    player.origin = time.monotonic()
    playback.start()
    assert playback.wait(5.0)

    assert len(player.times) == 3
    for index, expected in enumerate([0.0, 0.1, 0.2]):
        assert player.times[index] == pytest.approx(expected, abs=0.04), (
            f"step {index} drifted: {player.times}")


def test_looping_stays_on_schedule():
    player = SlowPlayer(cost=0.02)
    schedule = build_schedule([KeyTap("a"), Sleep(60)])
    playback = Playback(player, schedule, loops=4)
    player.origin = time.monotonic()
    playback.start()
    assert playback.wait(5.0)

    assert len(player.times) == 4
    for index in range(4):
        assert player.times[index] == pytest.approx(index * 0.06, abs=0.04)


def test_stop_interrupts_a_long_sleep_immediately():
    player = FakePlayer()
    schedule = build_schedule([KeyTap("a"), Sleep(30000), KeyTap("b")])
    playback = Playback(player, schedule)
    playback.start()

    deadline = time.time() + 2
    while time.time() < deadline and not player.calls:
        time.sleep(0.01)

    began = time.monotonic()
    playback.stop()
    assert playback.wait(2.0), "stop did not interrupt the 30s sleep"
    assert time.monotonic() - began < 1.0
    assert playback.was_stopped
    assert ("key_down", "b") not in player.calls


def test_stop_is_safe_from_another_thread():
    """The panic grab calls stop() from its own watch thread."""
    player = FakePlayer()
    schedule = build_schedule([KeyTap("a"), Sleep(5000), KeyTap("b")])
    playback = Playback(player, schedule)
    playback.start()
    threading.Timer(0.1, playback.stop).start()
    assert playback.wait(3.0)
    assert playback.was_stopped
    assert ("key_down", "b") not in player.calls


def test_a_trailing_sleep_is_not_waited_out_on_the_last_pass():
    """It sets where the next loop starts, so on the final pass there is nothing
    left for it to delay."""
    player = FakePlayer()
    playback = Playback(player, build_schedule([KeyTap("a"), Sleep(5000)]))
    began = time.monotonic()
    playback.start()
    assert playback.wait(2.0)
    assert time.monotonic() - began < 1.0
    assert not playback.was_stopped


def test_infinite_loop_runs_until_stopped():
    player = FakePlayer()
    playback = Playback(player, build_schedule([KeyTap("a"), Sleep(20)]), loops=0)
    playback.start()
    time.sleep(0.25)
    assert playback.is_running
    playback.stop()
    assert playback.wait(2.0)
    assert len(player.calls) >= 6, "should have looped several times"


def test_on_step_reports_the_loop_and_step_index():
    seen = []
    playback = Playback(
        FakePlayer(),
        build_schedule([KeyTap("a"), Sleep(10), KeyTap("b"), Sleep(10)]),
        loops=2,
        on_step=lambda loop, step, _: seen.append((loop, step)),
    )
    playback.start()
    assert playback.wait(3.0)
    assert seen == [(0, 0), (0, 1), (1, 0), (1, 1)]


def test_on_finish_reports_a_clean_run():
    result = []
    playback = Playback(
        FakePlayer(), build_schedule([KeyTap("a")]),
        on_finish=lambda stopped, error: result.append((stopped, error)))
    playback.start()
    assert playback.wait(3.0)
    assert result == [(False, None)]


def test_a_failing_player_surfaces_the_error_rather_than_swallowing_it():
    class Broken(FakePlayer):
        def key_down(self, sym):
            raise RuntimeError("no such key")

    result = []
    playback = Playback(
        Broken(), build_schedule([KeyTap("a")]),
        on_finish=lambda stopped, error: result.append((stopped, error)))
    playback.start()
    assert playback.wait(3.0)

    stopped, error = result[0]
    assert stopped is False
    assert isinstance(error, RuntimeError)
    assert not playback.is_running


def test_an_empty_schedule_finishes_immediately():
    result = []
    playback = Playback(
        FakePlayer(), build_schedule([]),
        on_finish=lambda stopped, error: result.append((stopped, error)))
    playback.start()
    assert playback.wait(2.0)
    assert result == [(False, None)]


def test_cannot_start_twice():
    playback = Playback(FakePlayer(), build_schedule([KeyTap("a")]))
    playback.start()
    with pytest.raises(RuntimeError, match="already started"):
        playback.start()
    playback.wait(2.0)
