"""GUI tests. The window runs against the real Qt event loop on the Xvfb display,
but with fake backends injected, so the state machine is exercised without the
timing of real capture and injection.
"""

from __future__ import annotations

import time

import pytest

from macrorec.backend.fake import FakePlayer, FakeRecorder
from macrorec.collapse import merge_sleeps
from macrorec.events import Click, KeyDown, KeyTap, KeyUp, Move, MoveRel, Sleep
from macrorec.script import format_macro, parse
from macrorec.settings import Settings
from macrorec.timeline import build_schedule

pytest.importorskip("PyQt5", reason="PyQt5 is not installed")

from PyQt5.QtGui import QKeySequence  # noqa: E402
from PyQt5.QtWidgets import QApplication  # noqa: E402

from macrorec import gui  # noqa: E402


@pytest.fixture(scope="session")
def qapp(xvfb):
    app = QApplication.instance() or QApplication([])
    yield app


class FakeGrab:
    """Stand-in for HotkeyGrab.

    Keys its bindings on the *canonical* hotkey, not the raw text, because the real
    grab keys on (keycode, modifier mask) and two spellings of one chord are one
    grab there. A fake that kept them apart would hide a collision the app hits.
    """

    def __init__(self):
        self.started = False
        self.bindings = {}

    def start(self, bindings):
        canonical = {}
        for spec, action in bindings.items():
            if not spec:
                continue
            key = gui._normalise_hotkey(spec)
            if key in canonical:
                raise RuntimeError(f"{spec!r} is the same combination as another")
            canonical[key] = action
        if not canonical:
            return
        self.started = True
        self.bindings = canonical

    def stop(self):
        self.started = False

    def fire(self, spec):
        """As the grab's watch thread would, on seeing that chord."""
        self.bindings[gui._normalise_hotkey(spec)]()

    @property
    def syms(self):
        return sorted(self.bindings)


@pytest.fixture
def window(qapp, tmp_path):
    """A window with fake backends. `harness` collects what they were handed."""
    harness = {
        "players": [], "recorders": [], "grabs": [], "game_grabs": [],
        "warnings": [], "recorder_capture_raw_input": [],
        "panic_key_is_withheld": [],
    }

    def recorder_factory(capture_raw_input):
        harness["recorder_capture_raw_input"].append(capture_raw_input)
        recorder = FakeRecorder(harness.get("script", []))
        harness["recorders"].append(recorder)
        return recorder

    def player_factory(skip_syms):
        player = FakePlayer(skip_syms)
        harness["players"].append(player)
        return player

    def grab_factory():
        grab = FakeGrab()
        harness["grabs"].append(grab)
        return grab

    def game_grab_factory():
        grab = FakeGrab()
        harness["game_grabs"].append(grab)
        return grab

    win = gui.MacroRecWindow(
        Settings(),
        recorder_factory=recorder_factory,
        player_factory=player_factory,
        grab_factory=grab_factory,
        game_grab_factory=game_grab_factory,
        warnings_factory=lambda macro, panic, panic_key_is_withheld: (
            harness.setdefault("warned_about", []).append(panic)
            or harness["panic_key_is_withheld"].append(panic_key_is_withheld)
            or harness["warnings"]),
        settings_path=str(tmp_path / "settings.json"),
    )
    win.show()
    qapp.processEvents()
    try:
        yield win, harness
    finally:
        win.close()
        qapp.processEvents()


def pump(qapp, predicate, timeout=3.0):
    """Run the Qt event loop until `predicate` holds. Signals emitted from worker
    threads only arrive while the loop is turning."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    qapp.processEvents()
    return predicate()


# --- state machine -----------------------------------------------------------


def test_starts_idle_with_nothing_to_play(window):
    win, _ = window
    assert win.mode == gui.IDLE
    assert win.record_button.isEnabled()
    assert not win.play_button.isEnabled(), "no macro loaded yet"
    assert not win.stop_button.isEnabled()
    assert win.status_label.text() == "0 steps"
    assert win.file_label.text() == "(unsaved macro)"


def test_reload_is_only_offered_once_there_is_a_file(window, qapp, tmp_path):
    win, _ = window
    assert not win.reload_action.isEnabled(), "nothing to reload yet"

    path = tmp_path / "sample.macro"
    path.write_text("version 1\n\nkey a\n")
    win.open_file(str(path))
    qapp.processEvents()
    assert win.reload_action.isEnabled()

    win.start_recording()  # recording drops the file association
    qapp.processEvents()
    win.stop()
    qapp.processEvents()
    assert not win.reload_action.isEnabled()


def test_record_and_play_are_mutually_exclusive(window, qapp):
    win, _ = window
    win.start_recording()
    qapp.processEvents()

    assert win.mode == gui.RECORDING
    assert not win.record_button.isEnabled()
    assert not win.play_button.isEnabled(), (
        "recording would capture our own injected events")
    assert win.stop_button.isEnabled()
    assert not win.open_action.isEnabled()

    win.stop()
    assert win.mode == gui.IDLE
    assert win.record_button.isEnabled()


def test_recording_collapses_motion_and_becomes_the_macro(window, qapp):
    win, harness = window
    harness["script"] = [
        (0.0, Move(1, 1)), (0.1, Move(9, 9)), (0.2, Click("left")),
        (0.5, KeyTap("a")),
    ]
    win.start_recording()
    assert pump(qapp, lambda: len(win._captured) >= 4)
    win.stop()
    qapp.processEvents()

    assert win.mode == gui.IDLE
    kinds = win.macro.events
    assert Move(9, 9) in kinds and Move(1, 1) not in kinds, "motion collapsed"
    assert any(isinstance(e, Sleep) for e in kinds), "timing preserved"
    assert win.play_button.isEnabled()


def test_a_fast_stroke_keeps_its_duration_in_the_default_mode(window, qapp):
    """The reported defect, through the window rather than the functions. The other
    collapse tests space their moves 100ms apart, so they pass whether or not
    sub-threshold gaps survive; XRecord really delivers motion every 1-3ms."""
    win, harness = window
    harness["script"] = ([(i * 0.002, Move(i, i)) for i in range(300)]
                         + [(0.600, Click("left"))])
    win.start_recording()
    assert pump(qapp, lambda: len(win._captured) >= 301)
    win.stop()
    qapp.processEvents()

    events = win.macro.events
    assert [e for e in events if not isinstance(e, Sleep)] == [
        Move(299, 299), Click("left"),
    ], "still collapsed to the endpoint"
    assert sum(isinstance(e, Sleep) for e in events) == 2, (
        "0.6s written as travel-then-dwell, not as 120 sleep lines"
    )
    assert build_schedule(events).duration == pytest.approx(0.6, abs=0.01)


def test_path_capture_keeps_the_intermediate_moves(window, qapp):
    """With the preference on, the route is the point: the moves a collapsed
    recording throws away are exactly the ones that have to survive."""
    win, harness = window
    win.settings.capture_motion_path = True
    harness["script"] = [
        (0.00, Move(1, 1)), (0.05, Move(5, 5)), (0.10, Move(9, 9)),
        (0.15, Click("left")),
    ]
    win.start_recording()
    assert pump(qapp, lambda: len(win._captured) >= 4)
    win.stop()
    qapp.processEvents()

    kinds = win.macro.events
    assert Move(1, 1) in kinds and Move(5, 5) in kinds and Move(9, 9) in kinds
    assert any(isinstance(e, Sleep) for e in kinds), "timing preserved"


def test_path_capture_thins_motion_at_the_real_capture_rate(window, qapp):
    """The other path-capture tests space their moves well over the sample interval,
    so they would pass even if sampling did nothing. XRecord really delivers motion
    every 1-3ms, and this is what fails if the sampling is ever moved to after
    to_events, where the sub-5ms gaps have already been thrown away."""
    win, harness = window
    win.settings.capture_motion_path = True
    harness["script"] = ([(i * 0.002, Move(i, i)) for i in range(100)]
                         + [(0.20, Click("left"))])
    win.start_recording()
    assert pump(qapp, lambda: len(win._captured) >= 101)
    win.stop()
    qapp.processEvents()

    moves = [e for e in win.macro.events if isinstance(e, Move)]
    assert 2 < len(moves) < 100, "thinned, but still a path"

    offsets = [step.at for step in build_schedule(win.macro.events)]
    assert offsets == sorted(offsets)
    assert len(set(offsets)) > 2, "the stroke is spread over time, not all at once"


def test_path_capture_is_off_unless_asked_for(window, qapp):
    """The same script, with the default settings, still collapses."""
    win, harness = window
    assert win.settings.capture_motion_path is False
    harness["script"] = [
        (0.00, Move(1, 1)), (0.05, Move(5, 5)), (0.10, Move(9, 9)),
        (0.15, Click("left")),
    ]
    win.start_recording()
    assert pump(qapp, lambda: len(win._captured) >= 4)
    win.stop()
    qapp.processEvents()

    assert Move(1, 1) not in win.macro.events


def test_path_capture_trims_the_whole_walk_to_our_stop_button(window, qapp):
    """Trimming the last move only removes one sample of the approach. The rest of
    the run would leave every path-captured macro ending by dragging the pointer
    onto the transport bar."""
    win, harness = window
    win.settings.capture_motion_path = True
    win.move(100, 100)
    qapp.processEvents()
    rect = win.frameGeometry()
    inside = rect.center()
    outside = (rect.right() + 400, rect.bottom() + 400)

    harness["script"] = [
        (0.00, KeyTap("a")),
        (0.05, Move(*outside)),
        (0.10, Move(inside.x() - 4, inside.y() - 4)),
        (0.15, Move(inside.x() - 2, inside.y() - 2)),
        (0.20, Move(inside.x(), inside.y())),
        (0.25, Click("left")),
    ]
    win.start_recording()
    assert pump(qapp, lambda: len(win._captured) >= 6)
    win.stop_button.click()
    qapp.processEvents()

    kinds = win.macro.events
    assert Move(*outside) in kinds, "motion outside our window is the user's"
    for point in ((inside.x() - 4, inside.y() - 4), (inside.x() - 2, inside.y() - 2),
                  (inside.x(), inside.y())):
        assert Move(*point) not in kinds, f"{point} walked to Stop, should be gone"
    assert Click("left") not in kinds


def test_path_capture_leaves_an_earlier_click_over_our_window_alone(window, qapp):
    """The walk-back stops at the first move outside our rect, so it only eats the
    approach contiguous with the click that ended the recording. A macro that
    genuinely clicks over this window earlier on keeps that."""
    win, harness = window
    win.settings.capture_motion_path = True
    win.move(100, 100)
    qapp.processEvents()
    rect = win.frameGeometry()
    inside = rect.center()
    outside = (rect.right() + 400, rect.bottom() + 400)

    harness["script"] = [
        (0.00, Move(inside.x() - 6, inside.y() - 6)),
        (0.05, Click("right")),
        (0.10, Move(*outside)),
        (0.15, Move(inside.x(), inside.y())),
        (0.20, Click("left")),
    ]
    win.start_recording()
    assert pump(qapp, lambda: len(win._captured) >= 5)
    win.stop_button.click()
    qapp.processEvents()

    kinds = win.macro.events
    assert Move(inside.x() - 6, inside.y() - 6) in kinds
    assert Click("right") in kinds
    assert Move(*outside) in kinds
    assert Move(inside.x(), inside.y()) not in kinds


# --- M2: capture_raw_input, moverel and the game-mode panic grab -------------


def test_recording_asks_for_the_raw_backend_when_the_setting_is_on(window, qapp):
    win, harness = window
    win.settings.capture_raw_input = True
    win.start_recording()
    qapp.processEvents()
    win.stop()
    qapp.processEvents()
    assert harness["recorder_capture_raw_input"] == [True]


def test_recording_asks_for_the_ordinary_backend_by_default(window, qapp):
    win, harness = window
    win.start_recording()
    qapp.processEvents()
    win.stop()
    qapp.processEvents()
    assert harness["recorder_capture_raw_input"] == [False]


def test_raw_capture_sums_deltas_instead_of_collapsing_them(window, qapp):
    """MoveRel must go through accumulate_motion, never collapse_motion: collapsing
    a run of deltas to its last one would silently discard the rest of the turn -
    the exact failure AGENTS.md warns path capture could reproduce if the ordering
    were ever gotten backwards."""
    win, harness = window
    win.settings.capture_raw_input = True
    harness["script"] = [
        (0.00, MoveRel(3, -1)), (0.01, MoveRel(4, -1)), (0.02, MoveRel(5, -1)),
        (0.03, Click("left")),
    ]
    win.start_recording()
    assert pump(qapp, lambda: len(win._captured) >= 4)
    win.stop()
    qapp.processEvents()

    moves = [e for e in win.macro.events if isinstance(e, MoveRel)]
    assert moves, "collapsed away instead of summed"
    assert sum(m.dx for m in moves) == 12
    assert sum(m.dy for m in moves) == -3


def test_playback_arms_the_game_grab_when_raw_capture_is_on(window, qapp):
    """A game's own exclusive keyboard grab would block HotkeyGrab's XGrabKey,
    so the panic stop has to move to the XI2 watcher while capture_raw_input is
    on - gated on the setting, per AGENTS.md, not unconditional."""
    win, harness = window
    win.settings.capture_raw_input = True
    win.macro = parse("key a\nsleep 3s\nkey b\n")
    win._refresh()
    win.start_playback()

    assert pump(qapp, lambda: win.mode == gui.PLAYING)
    assert harness["game_grabs"], "the game grab factory was never used"
    assert win._grab is harness["game_grabs"][-1], (
        "the window must be holding the game grab, not some other one")
    assert win._grab.started and win._grab.syms == ["Escape"]

    win.stop()
    assert pump(qapp, lambda: win.mode == gui.IDLE)


def test_playback_uses_the_ordinary_grab_by_default(window, qapp):
    win, harness = window
    assert win.settings.capture_raw_input is False
    win.macro = parse("key a\nsleep 3s\nkey b\n")
    win._refresh()
    win.start_playback()

    assert pump(qapp, lambda: win.mode == gui.PLAYING)
    assert harness["grabs"] and win._grab is harness["grabs"][-1]
    assert win._grab.started
    assert not harness["game_grabs"]

    win.stop()
    assert pump(qapp, lambda: win.mode == gui.IDLE)


def test_recording_still_uses_the_ordinary_grab_even_with_raw_capture_on(window, qapp):
    """capture_raw_input only moves the panic grab to XI2 during PLAYING - Record
    and Play hotkeys never need to survive a game's grab, since they are pressed
    before the game has one."""
    win, harness = window
    win.settings.capture_raw_input = True
    win.settings.record_key = "F9"
    win._rebind_hotkeys()
    assert harness["grabs"] and harness["grabs"][-1].started
    assert not harness["game_grabs"]


def test_raw_capture_withholds_nothing_from_the_panic_key(window, qapp):
    """With capture_raw_input on, RawHotkeyWatch filters its own injected keys by
    sourceid instead of warn-and-skip withholding them, so the panic key plays
    back like any other key and there is nothing to warn about - see AGENTS.md."""
    win, harness = window
    win.settings.capture_raw_input = True
    win.macro = parse("key Escape\n")
    win._refresh()
    win.start_playback()

    assert pump(qapp, lambda: win.mode == gui.IDLE)
    player = harness["players"][0]
    assert player.skip_syms == set(), "the panic key must not be withheld"
    assert ("key_down", "Escape") in player.calls
    assert harness["panic_key_is_withheld"][-1] is False
    assert harness["warnings"] == []


def test_ordinary_playback_still_withholds_the_panic_key(window, qapp):
    win, harness = window
    assert win.settings.capture_raw_input is False
    win.macro = parse("key Escape\n")
    win._refresh()
    win.start_playback()

    assert pump(qapp, lambda: win.mode == gui.IDLE)
    assert harness["players"][0].skip_syms == {"Escape"}
    assert harness["panic_key_is_withheld"][-1] is True


def test_playback_survives_a_game_grab_that_fails_to_arm(window, qapp, monkeypatch):
    """Fail-closed, seen from the GUI: if RawHotkeyWatch cannot arm (here
    standing in for a discovery failure), playback still starts, the failure is
    reported instead of raised, and no grab is held - never an unfiltered one."""
    win, harness = window
    win.settings.capture_raw_input = True

    def broken():
        raise RuntimeError("simulated: could not tell injected keys from real ones")

    monkeypatch.setattr(win, "_game_grab_factory", broken)
    shown = []
    monkeypatch.setattr(gui.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: shown.append(a)))

    win.macro = parse("key a\n")
    win._refresh()
    win.start_playback()

    assert pump(qapp, lambda: win.mode == gui.IDLE)
    assert win._grab is None
    assert shown, "a failed game grab must not be silent"


def test_the_raw_input_checkbox_is_seeded_from_settings(qapp):
    off = gui.SettingsDialog(Settings())
    assert off.raw_input_check.isChecked() is False

    on = gui.SettingsDialog(Settings(capture_raw_input=True))
    assert on.raw_input_check.isChecked() is True


def test_the_raw_input_checkbox_is_written_back_on_accept(qapp):
    settings = Settings()
    dialog = gui.SettingsDialog(settings)
    dialog.raw_input_check.setChecked(True)
    dialog.accept()
    assert dialog.result() == gui.QDialog.Accepted
    assert settings.capture_raw_input is True


def test_default_recorder_picks_the_raw_backend_against_a_real_display(xvfb):
    """The GUI tests above all go through fakes, which would stay green even if
    _default_recorder's own wiring to XI2Recorder were wrong. This exercises the
    factory function itself, relying on the xvfb fixture's $DISPLAY the same way
    the factory does."""
    xi2 = pytest.importorskip("macrorec.backend.xi2")
    recorder = gui._default_recorder(True)
    try:
        assert isinstance(recorder, xi2.XI2Recorder)
        recorder.start(lambda at, event: None)
        assert recorder.is_recording
    finally:
        recorder.stop()


def test_default_recorder_picks_the_ordinary_backend_against_a_real_display(xvfb):
    x11 = pytest.importorskip("macrorec.backend.x11")
    recorder = gui._default_recorder(False)
    try:
        assert isinstance(recorder, x11.X11Recorder)
        recorder.start(lambda at, event: None)
        assert recorder.is_recording
    finally:
        recorder.stop()


def test_default_game_grab_arms_against_a_real_display(xvfb):
    """Same reasoning as the recorder factory test above: _default_game_grab is
    never called by any fake-backed test, so its own construction of
    RawHotkeyWatch() needs its own check."""
    xi2 = pytest.importorskip("macrorec.backend.xi2")
    grab = gui._default_game_grab()
    assert isinstance(grab, xi2.RawHotkeyWatch)
    try:
        grab.start({"Escape": lambda: None})
        assert grab.is_active
        assert grab.grabbed == ["Escape"]
    finally:
        grab.stop()


def test_the_recording_counter_updates_while_recording(window, qapp):
    win, harness = window
    harness["script"] = [(0.0, KeyTap("a")), (0.1, KeyTap("b"))]
    win.start_recording()
    assert pump(qapp, lambda: "2 events" in win.status_label.text()), (
        f"counter never moved: {win.status_label.text()!r}")
    win.stop()
    qapp.processEvents()


def test_the_click_that_stops_recording_is_trimmed(window, qapp):
    """XRecord taps every client, so pressing Stop is captured like any other click.
    Left in, every macro would end by clicking wherever this window was."""
    win, harness = window
    win.move(100, 100)
    qapp.processEvents()
    centre = win.stop_button.mapToGlobal(win.stop_button.rect().center())

    harness["script"] = [
        (0.0, KeyTap("a")),
        (0.2, Move(centre.x(), centre.y())),
        (0.3, Click("left")),
    ]
    win.start_recording()
    assert pump(qapp, lambda: len(win._captured) >= 3)
    win.stop_button.click()  # the mouse ends the recording, as a user would
    qapp.processEvents()

    assert not any(isinstance(e, Move) for e in win.macro.events)
    assert not any(isinstance(e, Click) for e in win.macro.events)
    assert win.macro.events == [KeyTap("a")]


def test_a_click_inside_the_window_is_kept_when_stop_was_not_clicked(window, qapp):
    """Only a click that actually ended the recording is ours to remove. Stopped any
    other way, a click over our window is just part of the macro."""
    win, harness = window
    win.move(100, 100)
    qapp.processEvents()
    centre = win.stop_button.mapToGlobal(win.stop_button.rect().center())

    harness["script"] = [
        (0.0, KeyTap("a")),
        (0.2, Move(centre.x(), centre.y())),
        (0.3, Click("left")),
    ]
    win.start_recording()
    assert pump(qapp, lambda: len(win._captured) >= 3)
    win.stop()
    qapp.processEvents()

    assert Move(centre.x(), centre.y()) in win.macro.events
    assert Click("left") in win.macro.events


def test_a_click_outside_the_window_is_kept(window, qapp):
    win, harness = window
    win.move(100, 100)
    qapp.processEvents()
    far = win.frameGeometry().bottomRight()

    harness["script"] = [
        (0.0, KeyTap("a")),
        (0.2, Move(far.x() + 400, far.y() + 400)),
        (0.3, Click("left")),
    ]
    win.start_recording()
    assert pump(qapp, lambda: len(win._captured) >= 3)
    win.stop_button.click()
    qapp.processEvents()

    assert Move(far.x() + 400, far.y() + 400) in win.macro.events
    assert Click("left") in win.macro.events


def test_record_replaces_the_current_macro_without_prompting(window, qapp):
    win, harness = window
    win.macro = parse('key z\ntype "old"\n')
    win.path = "/tmp/previous.macro"
    win._refresh()

    harness["script"] = [(0.0, KeyTap("a"))]
    win.start_recording()
    assert pump(qapp, lambda: len(win._captured) >= 1)
    win.stop()
    qapp.processEvents()

    assert KeyTap("z") not in win.macro.events
    assert win.path is None, "the replacement is not the old file"
    assert win.file_label.text() == "(unsaved macro)"


# --- playback ----------------------------------------------------------------


def test_play_runs_the_macro_and_returns_to_idle(window, qapp):
    win, harness = window
    win.macro = parse("key a\nsleep 10ms\nclick left\n")
    win._refresh()
    win.start_playback()

    assert pump(qapp, lambda: win.mode == gui.IDLE)
    player = harness["players"][0]
    assert player.calls == [
        ("key_down", "a"), ("key_up", "a"),
        ("button_down", "left"), ("button_up", "left"),
    ]
    assert player.closed, "the display connection was released"


def test_the_panic_key_is_skipped_during_playback(window, qapp):
    win, harness = window
    win.settings.panic_key = "Escape"
    win.macro = parse("key Escape\nkey a\n")
    win._refresh()
    win.start_playback()

    assert pump(qapp, lambda: win.mode == gui.IDLE)
    assert harness["players"][0].skip_syms == {"Escape"}


def test_the_panic_grab_is_held_only_while_playing(window, qapp):
    win, _ = window
    win.macro = parse("key a\nsleep 3s\nkey b\n")
    win._refresh()
    win.start_playback()

    assert pump(qapp, lambda: win.mode == gui.PLAYING)
    grab = win._grab
    assert grab.started and grab.syms == ["Escape"]

    win.stop()
    assert pump(qapp, lambda: win.mode == gui.IDLE)
    assert not grab.started, "the grab must not outlive playback"


def test_the_panic_grab_stops_playback(window, qapp):
    win, harness = window
    win.macro = parse("key a\nsleep 30s\nkey b\n")
    win._refresh()
    win.start_playback()
    assert pump(qapp, lambda: win.mode == gui.PLAYING)

    win._grab.fire("Escape")  # as the grab thread would

    assert pump(qapp, lambda: win.mode == gui.IDLE)
    assert win.status_label.text() == "stopped"
    assert ("key_down", "b") not in harness["players"][0].calls


def test_the_panic_grab_is_armed_before_anything_is_injected(window, qapp):
    """Arming after playback starts leaves a window in which the macro is already
    driving another application and nothing can stop it."""
    win, harness = window
    win.macro = parse("key a\nsleep 2s\nkey b\n")
    win._refresh()

    armed_at = []
    original = win._rebind_hotkeys

    def spy():
        original()
        armed_at.append(len(harness["players"][0].calls)
                        if harness["players"] else 0)

    win._rebind_hotkeys = spy
    win.start_playback()
    assert pump(qapp, lambda: win.mode == gui.PLAYING)
    try:
        assert armed_at and armed_at[0] == 0, "events were injected before the grab"
    finally:
        win.stop()
        pump(qapp, lambda: win.mode == gui.IDLE)


def test_stop_during_a_long_sleep_is_immediate(window, qapp):
    win, harness = window
    win.macro = parse("key a\nsleep 30s\nkey b\n")
    win._refresh()
    win.start_playback()
    assert pump(qapp, lambda: win.mode == gui.PLAYING)

    began = time.monotonic()
    win.stop()
    assert pump(qapp, lambda: win.mode == gui.IDLE)
    assert time.monotonic() - began < 2.0
    assert ("key_down", "b") not in harness["players"][0].calls


def test_progress_text_reports_the_loop_pass(window, qapp):
    win, _ = window
    win.macro = parse("key a\nsleep 20ms\nkey b\nsleep 20ms\n")
    win.loop_spin.setValue(3)
    win._refresh()
    seen = []
    original = win._on_stepped

    def spy(loop, step):
        original(loop, step)
        seen.append(win.status_label.text())

    win._on_stepped = spy
    win.bridge.stepped.disconnect()
    win.bridge.stepped.connect(spy)

    win.start_playback()
    assert pump(qapp, lambda: win.mode == gui.IDLE, timeout=5.0)
    assert any("pass 1/3" in text for text in seen)
    assert any("pass 3/3" in text for text in seen)


def test_a_failing_player_reports_instead_of_hanging(window, qapp, monkeypatch):
    win, _ = window

    class Broken(FakePlayer):
        def key_down(self, sym):
            raise RuntimeError("no such key")

    monkeypatch.setattr(win, "_player_factory", lambda skip: Broken())
    monkeypatch.setattr(gui.QMessageBox, "critical",
                        staticmethod(lambda *a, **k: None))
    win.macro = parse("key a\n")
    win._refresh()
    win.start_playback()

    assert pump(qapp, lambda: win.mode == gui.IDLE)
    assert win.record_button.isEnabled(), "the window is usable again"


# --- global hotkeys ----------------------------------------------------------


def test_record_and_play_hotkeys_are_unbound_by_default(window, qapp):
    """A global grab takes the key away from every other program, so macrorec must
    not claim one nobody asked for."""
    win, _ = window
    assert win.settings.record_key == ""
    assert win.settings.play_key == ""
    assert win._hotkey_syms == {}
    assert win._grab is None, "nothing should be grabbed with no hotkeys set"


def test_bound_hotkeys_are_grabbed_while_idle(window, qapp):
    win, _ = window
    win.settings.record_key = "F9"
    win.settings.play_key = "F10"
    win._rebind_hotkeys()
    assert win._grab.syms == ["F10", "F9"]


def test_the_record_hotkey_starts_recording(window, qapp):
    win, harness = window
    win.settings.record_key = "F9"
    win._rebind_hotkeys()
    harness["script"] = [(0.0, KeyTap("a"))]

    win._grab.fire("F9")
    qapp.processEvents()
    assert win.mode == gui.RECORDING


def test_the_record_hotkey_also_stops_recording(window, qapp):
    win, harness = window
    win.settings.record_key = "F9"
    win._rebind_hotkeys()
    harness["script"] = [(0.0, KeyTap("a"))]

    win._grab.fire("F9")
    assert pump(qapp, lambda: len(win._captured) >= 1)
    assert win._grab.syms == ["F9"], "the key stays held so it can stop the take"

    win._grab.fire("F9")
    qapp.processEvents()
    assert win.mode == gui.IDLE


def test_the_hotkey_that_stops_a_recording_is_trimmed_from_it(window, qapp):
    """Grabbing a key routes it to us, but XRecord still sees it, so it lands in
    the macro like any other keystroke."""
    win, harness = window
    win.settings.record_key = "F9"
    win._rebind_hotkeys()
    harness["script"] = [
        (0.0, KeyDown("a")), (0.1, KeyUp("a")),
        (0.5, KeyDown("F9")), (0.6, KeyUp("F9")),
    ]

    win._grab.fire("F9")
    assert pump(qapp, lambda: len(win._captured) >= 4)
    win._grab.fire("F9")
    qapp.processEvents()

    # The F9 pair and the pause before it are gone; the timing inside the take
    # itself is untouched.
    assert win.macro.events == [KeyDown("a"), Sleep(100), KeyUp("a")]
    assert not any(getattr(e, "sym", None) == "F9" for e in win.macro.events)


def test_a_modified_stop_hotkey_is_trimmed_whole(window, qapp):
    """The chord's modifier keys are recorded too, so trimming only the final key
    leaves `keydown ctrl` / `keydown shift` baked into every macro."""
    win, harness = window
    win.settings.record_key = "Ctrl+Shift+F9"
    win._rebind_hotkeys()
    harness["script"] = [
        (0.0, KeyDown("a")), (0.1, KeyUp("a")),
        (0.5, KeyDown("Control_L")), (0.6, KeyDown("Shift_L")),
        (0.7, KeyDown("F9")), (0.8, KeyUp("F9")),
        (0.9, KeyUp("Shift_L")), (1.0, KeyUp("Control_L")),
    ]

    win._grab.fire("Ctrl+Shift+F9")
    assert pump(qapp, lambda: len(win._captured) >= 8)
    win._grab.fire("Ctrl+Shift+F9")
    qapp.processEvents()

    syms = {e.sym for e in win.macro.events if isinstance(e, (KeyDown, KeyUp))}
    assert syms == {"a"}, f"the stop chord survived: {win.macro.events}"
    assert win.macro.events == [KeyDown("a"), Sleep(100), KeyUp("a")]


def test_the_play_hotkey_starts_playback(window, qapp):
    win, harness = window
    win.settings.play_key = "F10"
    win._rebind_hotkeys()
    win.macro = parse("key a\n")
    win._refresh()

    win._grab.fire("F10")
    assert pump(qapp, lambda: harness["players"] and harness["players"][0].calls)
    assert pump(qapp, lambda: win.mode == gui.IDLE)


def test_the_play_hotkey_is_not_held_during_playback(window, qapp):
    """Only the panic key matters mid-playback, and holding Play then would let a
    stray press restart a macro that is already running."""
    win, _ = window
    win.settings.play_key = "F10"
    win._rebind_hotkeys()
    win.macro = parse("key a\nsleep 3s\nkey b\n")
    win._refresh()
    win.start_playback()

    assert pump(qapp, lambda: win.mode == gui.PLAYING)
    assert win._grab.syms == ["Escape"]
    win.stop()
    assert pump(qapp, lambda: win.mode == gui.IDLE)
    assert win._grab.syms == ["F10"], "the idle hotkeys come back afterwards"


def test_a_hotkey_that_cannot_be_grabbed_is_reported(window, qapp, monkeypatch):
    win, _ = window

    def broken():
        raise RuntimeError("another program already holds F9")

    monkeypatch.setattr(win, "_grab_factory", broken)
    shown = []
    monkeypatch.setattr(gui.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: shown.append(a)))

    win.settings.record_key = "F9"
    win._rebind_hotkeys()
    assert shown, "a hotkey that could not be registered must not be silent"
    assert "F9" in shown[0][2]


def test_grabs_are_released_when_the_window_closes(qapp, tmp_path):
    """Otherwise the key stays taken from the rest of the desktop."""
    grabs = []

    def grab_factory():
        grab = FakeGrab()
        grabs.append(grab)
        return grab

    win = gui.MacroRecWindow(
        Settings(record_key="F9"), grab_factory=grab_factory,
        recorder_factory=lambda capture_raw_input: FakeRecorder([]),
        player_factory=lambda skip: FakePlayer(),
        warnings_factory=lambda macro, panic, panic_key_is_withheld: [],
        settings_path=str(tmp_path / "settings.json"))
    win.show()
    qapp.processEvents()
    assert grabs and grabs[-1].started

    win.close()
    qapp.processEvents()
    assert not grabs[-1].started


# --- files -------------------------------------------------------------------


def test_open_load_and_reload(window, qapp, tmp_path):
    win, _ = window
    path = tmp_path / "sample.macro"
    path.write_text("# macro: sample\nversion 1\n\nkey a\nsleep 100ms\nkey b\n")

    win.open_file(str(path))
    qapp.processEvents()
    assert win.macro.name == "sample"
    assert len(win.macro.events) == 3
    assert win.file_label.text() == "sample.macro"
    assert "3 steps" in win.status_label.text()

    path.write_text("version 1\n\nkey c\n")
    win.reload_file()
    qapp.processEvents()
    assert win.macro.events == [KeyTap("c")], "reload re-read the edited file"


def test_a_malformed_file_is_reported_and_the_macro_is_untouched(
        window, qapp, tmp_path, monkeypatch):
    win, _ = window
    win.macro = parse("key a\n")
    win._refresh()

    shown = []
    monkeypatch.setattr(gui.QMessageBox, "critical",
                        staticmethod(lambda *a, **k: shown.append(a)))
    bad = tmp_path / "bad.macro"
    bad.write_text("key a\nwiggle 3\n")

    win.open_file(str(bad))
    qapp.processEvents()
    assert shown, "the parse error was reported"
    assert win.macro.events == [KeyTap("a")], "the loaded macro survived"
    assert win.path is None


def test_save_round_trips_through_the_parser(window, qapp, tmp_path):
    win, _ = window
    win.macro = parse('key Return\nsleep 250ms\ntype "hello"\n')
    win.path = str(tmp_path / "out.macro")
    win.speed_spin.setValue(2.0)
    win._refresh()

    win.save_file()
    written = (tmp_path / "out.macro").read_text()
    reparsed = parse(written)
    assert reparsed.events == win.macro.events
    assert reparsed.speed == 2.0, "the speed control is written to the header"
    assert written == format_macro(win.macro)


def test_the_speed_header_seeds_the_control_on_load(window, qapp, tmp_path):
    win, _ = window
    path = tmp_path / "fast.macro"
    path.write_text("version 1\nspeed 3\n\nkey a\n")
    win.open_file(str(path))
    qapp.processEvents()
    assert win.speed_spin.value() == pytest.approx(3.0)


def test_warnings_are_surfaced_on_load(window, qapp, tmp_path, monkeypatch):
    win, harness = window
    harness["warnings"] = ["macro contains the panic key 'Escape'"]
    shown = []
    monkeypatch.setattr(gui.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: shown.append(a)))

    path = tmp_path / "panic.macro"
    path.write_text("version 1\n\nkey Escape\n")
    win.open_file(str(path))
    qapp.processEvents()
    assert shown and "panic key" in shown[0][2]


# --- settings ----------------------------------------------------------------


def test_settings_persist_on_close(qapp, tmp_path):
    path = str(tmp_path / "settings.json")
    win = gui.MacroRecWindow(Settings(), settings_path=path)
    win.show()
    qapp.processEvents()
    win.loop_spin.setValue(7)
    win.speed_spin.setValue(1.5)
    win.on_top_check.setChecked(False)
    win.close()
    qapp.processEvents()

    reloaded = Settings.load(path)
    assert reloaded.loops == 7
    assert reloaded.speed == pytest.approx(1.5)
    assert reloaded.always_on_top is False


def test_settings_dialog_rejects_an_empty_panic_key(qapp, monkeypatch):
    settings = Settings()
    dialog = gui.SettingsDialog(settings, key_check=lambda sym: True)
    monkeypatch.setattr(gui.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: None))

    dialog.panic_edit.setText("   ")
    dialog.accept()
    assert dialog.result() != gui.QDialog.Accepted
    assert settings.panic_key == "Escape"

    dialog.panic_edit.setText("F12")
    dialog.accept()
    assert settings.panic_key == "F12"


def test_settings_dialog_rejects_a_key_the_keyboard_cannot_produce(
        qapp, monkeypatch):
    """An unresolvable panic key means the grab never arms, which leaves a running
    macro with no stop outside this window."""
    settings = Settings()
    dialog = gui.SettingsDialog(settings, key_check=lambda sym: sym == "F12")
    shown = []
    monkeypatch.setattr(gui.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: shown.append(a)))

    dialog.panic_edit.setText("Nonsense_Key")
    dialog.accept()
    assert dialog.result() != gui.QDialog.Accepted
    assert settings.panic_key == "Escape", "the bad key was not stored"
    assert shown and "never fire" in shown[0][2]

    dialog.panic_edit.setText("F12")
    dialog.accept()
    assert settings.panic_key == "F12"


def test_the_real_key_check_accepts_escape_and_rejects_nonsense(xvfb):
    pytest.importorskip("macrorec.backend.x11")
    assert gui._default_key_check("Escape") is True
    assert gui._default_key_check("Nonsense_Key") is False


def test_window_shortcuts_are_listed_in_the_settings_dialog(qapp):
    """They used to be literals at the call site, invisible to the dialog that
    calls itself the keybind settings."""
    settings = Settings()
    dialog = gui.SettingsDialog(settings, key_check=lambda spec: True)
    assert set(dialog.window_edits) == {
        "open_key", "save_key", "save_as_key", "reload_key"}
    assert dialog.window_edits["open_key"].text() == "Ctrl+O"
    assert dialog.window_edits["save_as_key"].text() == "Ctrl+Shift+S"


def test_the_motion_path_checkbox_is_seeded_from_settings(qapp):
    dialog = gui.SettingsDialog(Settings(), key_check=lambda spec: True)
    assert dialog.motion_path_check.isChecked() is False

    on = gui.SettingsDialog(Settings(capture_motion_path=True),
                            key_check=lambda spec: True)
    assert on.motion_path_check.isChecked() is True


def test_the_motion_path_checkbox_is_written_back_on_accept(qapp):
    settings = Settings()
    dialog = gui.SettingsDialog(settings, key_check=lambda spec: True)
    dialog.motion_path_check.setChecked(True)
    dialog.accept()

    assert dialog.result() == gui.QDialog.Accepted
    assert settings.capture_motion_path is True


def test_a_rejected_keybind_leaves_the_motion_path_setting_untouched(qapp, monkeypatch):
    """It is assigned after every early return, like the keybinds it sits beside."""
    monkeypatch.setattr(gui.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: None))
    settings = Settings()
    dialog = gui.SettingsDialog(settings, key_check=lambda spec: False)
    dialog.motion_path_check.setChecked(True)
    dialog.accept()

    assert settings.capture_motion_path is False


def test_the_motion_path_setting_persists_on_close(qapp, tmp_path):
    path = str(tmp_path / "settings.json")
    win = gui.MacroRecWindow(Settings(), settings_path=path)
    win.settings.capture_motion_path = True
    win.close()
    qapp.processEvents()

    assert Settings.load(path).capture_motion_path is True


def test_window_shortcuts_come_from_settings_not_from_literals(window, qapp):
    win, _ = window
    assert win.open_action.shortcut() == QKeySequence("Ctrl+O")
    assert win.reload_action.shortcut() == QKeySequence("Ctrl+R")

    win.settings.reload_key = "F5"
    win._apply_shortcuts()
    assert win.reload_action.shortcut() == QKeySequence("F5")


def test_a_window_shortcut_can_be_unbound(window, qapp):
    win, _ = window
    win.settings.save_key = ""
    win._apply_shortcuts()
    assert win.save_action.shortcut().isEmpty()


def test_changing_a_window_shortcut_takes_effect_immediately(window, qapp,
                                                             monkeypatch):
    win, _ = window

    def run_dialog(self):
        self.window_edits["reload_key"].setText("F5")
        self.accept()
        return gui.QDialog.Accepted

    monkeypatch.setattr(gui.SettingsDialog, "exec_", run_dialog)
    monkeypatch.setattr(gui, "_default_key_check", lambda spec: True)
    win.open_settings()
    qapp.processEvents()

    assert win.settings.reload_key == "F5"
    assert win.reload_action.shortcut() == QKeySequence("F5")


def test_settings_dialog_rejects_an_unparseable_window_shortcut(qapp, monkeypatch):
    settings = Settings()
    dialog = gui.SettingsDialog(settings, key_check=lambda spec: True)
    shown = []
    monkeypatch.setattr(gui.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: shown.append(a)))

    dialog.window_edits["open_key"].setText("Ctrl+Nonsense")
    dialog.accept()
    assert dialog.result() != gui.QDialog.Accepted
    assert shown and "not a shortcut Qt understands" in shown[0][2]
    assert settings.open_key == "Ctrl+O", "the bad value was not stored"


def test_a_global_hotkey_clashing_with_a_window_shortcut_is_refused(
        qapp, monkeypatch):
    """The grab intercepts the key before Qt sees it, so the window shortcut would
    simply never fire. Silently keeping both would look like a broken menu item."""
    settings = Settings()
    dialog = gui.SettingsDialog(settings, key_check=lambda spec: True)
    shown = []
    monkeypatch.setattr(gui.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: shown.append(a)))

    dialog.panic_edit.setText("Escape")
    dialog.record_edit.setText("Ctrl+R")  # already Reload's window shortcut
    dialog.accept()
    assert dialog.result() != gui.QDialog.Accepted
    assert shown and "same combination" in shown[0][2]


def test_the_real_key_check_understands_modifiers(xvfb):
    pytest.importorskip("macrorec.backend.x11")
    assert gui._default_key_check("Ctrl+Shift+A") is True
    assert gui._default_key_check("Alt+F4") is True
    assert gui._default_key_check("Hyper+A") is False, "unknown modifier"
    assert gui._default_key_check("Ctrl+Nonsense_Key") is False


def test_settings_dialog_stores_a_canonical_spelling(qapp, monkeypatch):
    settings = Settings()
    dialog = gui.SettingsDialog(settings, key_check=lambda spec: True)
    dialog.panic_edit.setText("Escape")
    dialog.record_edit.setText("  shift+ctrl+F9 ")
    dialog.accept()

    assert dialog.result() == gui.QDialog.Accepted
    assert settings.record_key == "Ctrl+Shift+F9", (
        "hotkeys are normalised, so settings.json does not depend on typing style")


def test_settings_dialog_rejects_an_unparseable_hotkey(qapp, monkeypatch):
    settings = Settings()
    dialog = gui.SettingsDialog(settings, key_check=lambda spec: True)
    shown = []
    monkeypatch.setattr(gui.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: shown.append(a)))

    dialog.record_edit.setText("Hyper+Shft+A")
    dialog.accept()
    assert dialog.result() != gui.QDialog.Accepted
    assert shown and "could not be understood" in shown[0][2]
    assert settings.record_key == ""


def test_settings_dialog_catches_a_clash_written_two_ways(qapp, monkeypatch):
    """`ctrl+a` and `Ctrl+A` are the same hotkey, so comparing raw text would miss
    the collision and one of them would silently never fire."""
    settings = Settings()
    dialog = gui.SettingsDialog(settings, key_check=lambda spec: True)
    shown = []
    monkeypatch.setattr(gui.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: shown.append(a)))

    dialog.panic_edit.setText("Escape")
    dialog.record_edit.setText("ctrl+F9")
    dialog.play_edit.setText("CTRL+F9")
    dialog.accept()
    assert dialog.result() != gui.QDialog.Accepted
    assert shown and "same combination" in shown[0][2]


def test_a_modified_panic_key_does_not_suppress_the_bare_key(window, qapp):
    """With Ctrl+Escape as the panic stop, a macro's plain Escape cannot trigger it,
    so withholding Escape from playback would break the macro for nothing."""
    win, harness = window
    win.settings.panic_key = "Ctrl+Escape"
    win.macro = parse("key Escape\n")
    win._refresh()
    win.start_playback()

    assert pump(qapp, lambda: win.mode == gui.IDLE)
    player = harness["players"][0]
    assert player.skip_syms == set(), "nothing should be withheld"
    assert ("key_down", "Escape") in player.calls


def test_an_unmodified_panic_key_still_suppresses_it(window, qapp):
    win, harness = window
    win.settings.panic_key = "Escape"
    win.macro = parse("key Escape\nkey a\n")
    win._refresh()
    win.start_playback()

    assert pump(qapp, lambda: win.mode == gui.IDLE)
    player = harness["players"][0]
    assert player.skip_syms == {"Escape"}
    assert ("key_down", "Escape") not in player.calls


def test_the_configured_panic_key_reaches_the_warning_check(window, qapp):
    """Not Escape. The skip list, the grab and the load-time warning must all agree
    on which key is the panic key."""
    win, harness = window
    win.settings.panic_key = "F12"
    win.macro = parse("key a\n")
    win._refresh()
    win.start_playback()

    assert pump(qapp, lambda: win.mode == gui.IDLE)
    assert harness["warned_about"] == ["F12"]
    assert harness["players"][0].skip_syms == {"F12"}
    playing_grab = [g for g in harness["grabs"] if g.syms == ["F12"]]
    assert playing_grab, "the grab held during playback was not the configured key"


def test_a_grab_that_will_not_arm_is_reported_not_swallowed(
        window, qapp, monkeypatch):
    win, _ = window

    def broken(sym):
        raise RuntimeError("Escape is already grabbed")

    monkeypatch.setattr(win, "_grab_factory", broken)
    shown = []
    monkeypatch.setattr(gui.QMessageBox, "warning",
                        staticmethod(lambda *a, **k: shown.append(a)))

    win.macro = parse("key a\n")
    win._refresh()
    win.start_playback()
    assert pump(qapp, lambda: win.mode == gui.IDLE)

    assert shown, "a failed panic grab must not be silent"
    assert "panic stop could not be armed" in shown[0][2]


# --- against the real backend ------------------------------------------------


def test_the_window_records_and_replays_through_the_real_x_backend(
        qapp, xvfb, tmp_path):
    """No fakes: the window's own X11Recorder captures injected input, the macro is
    saved and reloaded from disk, and the window's X11Player replays it."""
    x11 = pytest.importorskip("macrorec.backend.x11")

    win = gui.MacroRecWindow(
        Settings(), settings_path=str(tmp_path / "settings.json"))
    win.show()
    qapp.processEvents()
    try:
        win.start_recording()
        assert win.mode == gui.RECORDING

        source = x11.X11Player()
        source.perform(KeyTap("a"))
        source.perform(Move(200, 150))
        source.perform(Click("left"))
        source.close()

        # 5 events: key down/up, one motion, button down/up.
        assert pump(qapp, lambda: len(win._captured) >= 5)
        win.stop()
        qapp.processEvents()
        assert win.mode == gui.IDLE

        recorded = list(win.macro.events)
        assert KeyTap("a") not in recorded, "the recorder emits down/up, not taps"
        assert any(getattr(e, "sym", None) == "a" for e in recorded)
        assert Move(200, 150) in recorded, "the click position survived collapse"

        path = tmp_path / "captured.macro"
        win.path = str(path)
        win._refresh()
        win.save_file()
        assert path.exists()

        win.macro = parse("key z\n")  # prove the reload really re-reads
        win.open_file(str(path))
        qapp.processEvents()
        assert win.macro.events == recorded
        assert merge_sleeps(recorded) == recorded, "sleeps left merged on the way out"

        # Replay it, watching with an independent recorder.
        watcher = x11.X11Recorder(xvfb.name)
        seen = []
        watcher.start(lambda at, event: seen.append(event))
        try:
            win.start_playback()
            assert pump(qapp, lambda: win.mode == gui.IDLE, timeout=10.0)
            assert pump(qapp, lambda: len(seen) >= 5, timeout=5.0)
        finally:
            watcher.stop()

        from macrorec.collapse import collapse_motion
        # Sleeps filtered out on both sides: this asserts the events and their order,
        # and `seen` is a raw capture that has none. The recording's own sleeps depend
        # on how fast five XTEST injections happened to land, which is not a property
        # worth pinning here.
        assert ([e for e in collapse_motion(seen) if not isinstance(e, Sleep)]
                == [e for e in recorded if not isinstance(e, Sleep)])
    finally:
        win.close()
        qapp.processEvents()


def test_always_on_top_toggle_sets_the_window_flag(window, qapp):
    from PyQt5.QtCore import Qt

    win, _ = window
    win.on_top_check.setChecked(True)
    qapp.processEvents()
    assert win.windowFlags() & Qt.WindowStaysOnTopHint
    win.on_top_check.setChecked(False)
    qapp.processEvents()
    assert not (win.windowFlags() & Qt.WindowStaysOnTopHint)


def test_toggling_always_on_top_never_hides_the_window(window, qapp):
    """setWindowFlag() re-parents the widget, which Qt does by hiding it. Reading
    isVisible() after the call therefore always says False, so a guard written that
    way skips the re-show and the program vanishes."""
    win, _ = window
    assert win.isVisible()

    for state in (False, True, False, True):
        win.on_top_check.setChecked(state)
        qapp.processEvents()
        assert win.isVisible(), (
            f"the window disappeared when 'on top' was set to {state}")
