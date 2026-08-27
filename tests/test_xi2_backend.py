"""XI2 raw input: does it see the mouselook a fullscreen game's pointer grab and
warp-to-centre hide from core `MotionNotify`, and does `XI2Recorder` capture it
correctly? See `~/.claude/plans/linux-macrorec-plan-capturing-delegated-eich.md`.
The first half is the M0 spike (wire layout, both go/no-go gates, the load-bearing
grab+warp test); the second half is M1's `XI2Recorder`.

Assertions are on what the XI2 tap observed, never on a client window receiving
input, matching AGENTS.md's rule for the core-protocol backend and for the same
reason: Xvfb has no window manager, so nothing but the tap can be trusted.
"""

from __future__ import annotations

import select
import time

import pytest

xinput = pytest.importorskip("Xlib.ext.xinput")
xi2 = pytest.importorskip("macrorec.backend.xi2")

from Xlib import X, XK, display  # noqa: E402
from Xlib.ext import xtest  # noqa: E402

from macrorec.events import KeyDown, KeyUp, MouseDown, MouseUp, MoveRel, Scroll  # noqa: E402
from macrorec.backend.x11 import GrabUnavailable  # noqa: E402


def _connect(xvfb):
    return display.Display(xvfb.name)


def _drain(dpy, predicate, timeout=3.0):
    """Every event matching `predicate` seen within `timeout`, in arrival order."""
    fileno = dpy.fileno()
    deadline = time.time() + timeout
    found = []
    while time.time() < deadline:
        readable, _, _ = select.select([fileno], [], [], 0.1)
        if not readable:
            continue
        for _ in range(dpy.pending_events()):
            event = dpy.next_event()
            if predicate(event):
                found.append(event)
    return found


def _is_raw(evtype):
    return lambda event: getattr(event, "evtype", None) == evtype


# --- wire layout, measured against this Xlib version and this server ---------


def test_raw_motion_decodes_the_injected_delta_exactly(xvfb):
    watch = _connect(xvfb)
    inject = _connect(xvfb)
    try:
        xi2.register(watch)
        xi2.select_raw_events(watch, mask=xinput.RawMotionMask)

        xtest.fake_input(inject, X.MotionNotify, detail=1, x=37, y=-19)
        inject.sync()

        events = _drain(watch, _is_raw(xinput.RawMotion))
        assert events, "no RawMotion observed"
        raw = events[0].data
        assert isinstance(raw, xi2.RawEvent)
        assert xi2.axis(raw.mask, raw.axisvalues_raw, xi2.AXIS_X) == 37
        assert xi2.axis(raw.mask, raw.axisvalues_raw, xi2.AXIS_Y) == -19
    finally:
        watch.close()
        inject.close()


def test_raw_key_press_carries_the_keycode_and_no_axes(xvfb):
    watch = _connect(xvfb)
    inject = _connect(xvfb)
    try:
        xi2.register(watch)
        xi2.select_raw_events(
            watch, mask=xinput.RawKeyPressMask | xinput.RawKeyReleaseMask)

        keycode = inject.keysym_to_keycode(ord("a"))
        xtest.fake_input(inject, X.KeyPress, keycode)
        inject.sync()

        events = _drain(watch, _is_raw(xinput.RawKeyPress))
        assert events, "no RawKeyPress observed"
        raw = events[0].data
        assert raw.detail == keycode
        assert raw.sourceid in xi2.xtest_device_ids(watch), (
            "an XTEST-injected key must carry an XTEST device as its sourceid")
        assert raw.mask == 0
        assert raw.axisvalues == ()
        assert raw.axisvalues_raw == ()
    finally:
        watch.close()
        inject.close()


# --- sourceid: telling XTEST injection from real input -----------------------


def test_xtest_device_ids_finds_the_xtest_slaves(xvfb):
    """Non-vacuous on both sides: a function that always returned frozenset()
    would leave every filtering test below green for the wrong reason."""
    dpy = _connect(xvfb)
    try:
        ids = xi2.xtest_device_ids(dpy)
        assert ids, "found no XTEST devices at all"
        devices = {d.deviceid: d.name for d in dpy.xinput_query_device(xinput.AllDevices).devices}
        for device_id in ids:
            assert "XTEST" in devices[device_id], (
                f"device {device_id} ({devices[device_id]!r}) has no XTEST in its name")
    finally:
        dpy.close()


def test_xtest_sourceid_is_stable_ungrabbed_and_grabbed(xvfb):
    """The measurement Stage 0 of the sourceid-filtering plan rests on: both the
    slave-attributed copy and the master-attributed echo of an ungrabbed XTEST
    injection carry the XTEST slave's id as `sourceid`, not the master's own -
    see xi2.py's module docstring. So a sourceid filter is safe even without a
    grab, which is the configuration a user tries first with capture_raw_input
    on and no game running."""
    watch = _connect(xvfb)
    inject = _connect(xvfb)
    try:
        xi2.register(watch)
        xi2.select_raw_events(watch, mask=xinput.RawKeyPressMask)
        xtest_ids = xi2.xtest_device_ids(watch)

        keycode = inject.keysym_to_keycode(ord("a"))
        xtest.fake_input(inject, X.KeyPress, keycode)
        inject.sync()
        ungrabbed = _drain(watch, _is_raw(xinput.RawKeyPress))
        assert ungrabbed, "no RawKeyPress observed ungrabbed"
        for event in ungrabbed:
            assert event.data.sourceid in xtest_ids

        grabber = _connect(xvfb)
        try:
            status = grabber.screen().root.grab_keyboard(
                False, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime)
            grabber.sync()
            assert status == 0, "setup: could not grab the keyboard at all"

            xtest.fake_input(inject, X.KeyPress, keycode)
            inject.sync()
            grabbed = _drain(watch, _is_raw(xinput.RawKeyPress))
            assert grabbed, "no RawKeyPress observed grabbed"
            for event in grabbed:
                assert event.data.sourceid in xtest_ids
        finally:
            grabber.ungrab_keyboard(X.CurrentTime)
            grabber.close()
    finally:
        watch.close()
        inject.close()


# --- M0 gate 1: raw keys under an exclusive keyboard grab --------------------


def test_raw_key_press_survives_an_exclusive_keyboard_grab(xvfb):
    """Load-bearing for moving the panic stop to XI2 (M2): if this fails, the
    panic-stop migration needs re-deciding before M2, per the plan."""
    watch = _connect(xvfb)
    grabber = _connect(xvfb)
    inject = _connect(xvfb)
    try:
        xi2.register(watch)
        xi2.select_raw_events(watch, mask=xinput.RawKeyPressMask)

        status = grabber.screen().root.grab_keyboard(
            False, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime)
        grabber.sync()
        assert status == 0, "setup: could not grab the keyboard at all"

        keycode = inject.keysym_to_keycode(ord("a"))
        xtest.fake_input(inject, X.KeyPress, keycode)
        inject.sync()

        events = _drain(watch, _is_raw(xinput.RawKeyPress))
        assert events, "RawKeyPress did not survive an exclusive keyboard grab"
    finally:
        grabber.ungrab_keyboard(X.CurrentTime)
        watch.close()
        grabber.close()
        inject.close()


# --- M0 gate 2: a modifier chord is reconstructible from raw keycodes alone --


def test_a_modifier_chord_is_reconstructible_from_raw_keycodes(xvfb):
    """The raw payload has no modifier field (unlike `xinput.DeviceEventData`,
    which carries one) - proves the state can still be tracked by hand from
    press/release, which is what a `Ctrl+Escape` panic stop needs in game mode
    (M2). Not yet built as reusable code; this is the feasibility proof."""
    watch = _connect(xvfb)
    inject = _connect(xvfb)
    try:
        xi2.register(watch)
        xi2.select_raw_events(
            watch, mask=xinput.RawKeyPressMask | xinput.RawKeyReleaseMask)

        ctrl_keycode = inject.keysym_to_keycode(XK.string_to_keysym("Control_L"))
        a_keycode = inject.keysym_to_keycode(XK.string_to_keysym("a"))

        xtest.fake_input(inject, X.KeyPress, ctrl_keycode)
        inject.sync()
        xtest.fake_input(inject, X.KeyPress, a_keycode)
        inject.sync()
        xtest.fake_input(inject, X.KeyRelease, a_keycode)
        inject.sync()
        xtest.fake_input(inject, X.KeyRelease, ctrl_keycode)
        inject.sync()

        held = set()
        chord_when_a_pressed = None
        seen = set()
        key_events = [
            e for e in _drain(
                watch,
                lambda e: getattr(e, "evtype", None) in
                (xinput.RawKeyPress, xinput.RawKeyRelease))
        ]
        for event in sorted(key_events, key=lambda e: e.data.time):
            raw = event.data
            key = (raw.deviceid, event.evtype, raw.detail)
            if key in seen:
                continue  # the XTEST slave and the core keyboard both report it
            seen.add(key)
            if event.evtype == xinput.RawKeyPress:
                if raw.detail == ctrl_keycode:
                    held.add(raw.detail)
                if raw.detail == a_keycode:
                    chord_when_a_pressed = frozenset(held)
            elif event.evtype == xinput.RawKeyRelease:
                held.discard(raw.detail)

        assert chord_when_a_pressed == frozenset({ctrl_keycode})
    finally:
        watch.close()
        inject.close()


# --- the load-bearing test: this is why the backend exists -------------------


def test_raw_motion_recovers_a_turn_that_grab_and_warp_hide_from_core(xvfb):
    """Mirrors the real `fps_rec.txt` bug: a client grabs the pointer and warps it
    back to centre after every move, exactly like a game's mouselook. The core
    trail should net to zero (the bug, reproduced on purpose) while XI2 recovers
    the true summed displacement (the fix).

    Asserting only the final `query_pointer()` position is vacuous: the last thing
    the loop does is warp there, so it would read centre even if XTEST injected
    nothing at all. The excursion the grabber's own core events observed *before*
    each warp is what proves real motion happened and was then erased - that is
    the actual bug, not a stand-in for it.
    """
    watch = _connect(xvfb)
    grabber = _connect(xvfb)
    inject = _connect(xvfb)
    try:
        xi2.register(watch)
        xi2.select_raw_events(watch, mask=xinput.RawMotionMask)

        root = grabber.screen().root
        centre_x, centre_y = 512, 384
        root.warp_pointer(centre_x, centre_y)
        grabber.sync()

        status = root.grab_pointer(
            False, X.PointerMotionMask, X.GrabModeAsync, X.GrabModeAsync,
            X.NONE, X.NONE, X.CurrentTime)
        grabber.sync()
        assert status == 0, "setup: could not grab the pointer at all"

        deltas = [(25, -10), (-8, 3), (14, 14), (-31, 2)]
        max_excursion = 0
        for dx, dy in deltas:
            xtest.fake_input(inject, X.MotionNotify, detail=1, x=dx, y=dy)
            inject.sync()
            # The grabbing client's own core view, the signal a game would act
            # on, observed *before* warping back to centre - this is the
            # excursion a vacuous final-position check would miss entirely.
            moves = _drain(grabber, lambda e: e.type == X.MotionNotify, timeout=1.0)
            for move in moves:
                max_excursion = max(
                    max_excursion,
                    abs(move.root_x - centre_x), abs(move.root_y - centre_y))
            root.warp_pointer(centre_x, centre_y)
            grabber.sync()

        assert max_excursion > 0, (
            "setup bug: the grabbing client never observed the pointer move "
            "off centre, so this isn't exercising the warp-erases-motion "
            "scenario at all")

        final = grabber.screen().root.query_pointer()
        core_net = (final.root_x - centre_x, final.root_y - centre_y)
        assert core_net == (0, 0), (
            "setup bug: the core trail should net to zero here, same as "
            "fps_rec.txt, or this test is not reproducing the scenario")

        raw_events = [e.data for e in _drain(watch, _is_raw(xinput.RawMotion))]
        devices = {r.deviceid for r in raw_events}
        # Measured 2026-08-22: under an active pointer grab, only the source
        # slave reports RawMotion for an XTEST-injected move - unlike the
        # ungrabbed case, where both the XTEST slave and the master pointer
        # each report it and naively summing all raw events double-counts.
        # Asserting the count here means a future change in that XI2 delivery
        # behaviour fails loudly instead of silently doubling every replayed
        # turn once M1 sums these deltas.
        assert len(devices) == 1, (
            f"expected RawMotion from exactly one device under a pointer "
            f"grab, got {devices}; summing across all of them would "
            f"silently double-count")

        raw_dx = sum(xi2.axis(r.mask, r.axisvalues_raw, xi2.AXIS_X) for r in raw_events)
        raw_dy = sum(xi2.axis(r.mask, r.axisvalues_raw, xi2.AXIS_Y) for r in raw_events)

        want_dx = sum(dx for dx, _ in deltas)
        want_dy = sum(dy for _, dy in deltas)
        assert (raw_dx, raw_dy) == (want_dx, want_dy)
    finally:
        try:
            grabber.ungrab_pointer(X.CurrentTime)
            grabber.sync()
        except Exception:
            pass
        watch.close()
        grabber.close()
        inject.close()


# --- M1: XI2Recorder -----------------------------------------------------------


def _settle(events, expected, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline and len(events) < expected:
        time.sleep(0.02)
    return [event for _, event in events]


def test_fractional_axis_values_are_carried_not_rounded_away():
    """Advisor-caught: rounding each raw sample to the nearest int independently
    would silently drop up to half a unit of real motion per sample. Five
    sub-half-unit deltas (0.4 each) round to zero individually - naive
    per-sample rounding would lose the whole 2.0 units of real displacement -
    but the carried remainder recovers the exact total. No display needed:
    `_translate_motion` only touches the recorder's own carry state."""
    recorder = xi2.XI2Recorder()
    total_dx = 0
    for _ in range(5):
        payload = xi2.RawEvent(
            deviceid=1, time=0, detail=0, sourceid=1, mask=0b11,
            axisvalues=(0.4, 0.0), axisvalues_raw=(0.4, 0.0))
        moved = recorder._translate_motion(payload)
        if moved is not None:
            total_dx += moved.dx
    assert total_dx == 2
    naive_total = sum(round(0.4) for _ in range(5))
    assert naive_total == 0, "the contrast this test exists to show"


def test_a_sample_that_rounds_to_zero_still_keeps_its_place_in_time():
    """A sub-half-unit sample can round away to nothing, but the sample still
    happened at a real instant. Dropping it from the sink (returning None)
    would drop that instant from `accumulate_motion`'s view entirely - the
    displacement survives via the carry, but the timing would not."""
    recorder = xi2.XI2Recorder()
    payload = xi2.RawEvent(
        deviceid=1, time=0, detail=0, sourceid=1, mask=0b11,
        axisvalues=(0.1, 0.1), axisvalues_raw=(0.1, 0.1))
    moved = recorder._translate_motion(payload)
    assert moved == MoveRel(0, 0)


def test_recorder_refuses_to_start_twice(xvfb):
    recorder = xi2.XI2Recorder(xvfb.name)
    events = []
    recorder.start(lambda at, event: events.append((at, event)))
    try:
        assert recorder.is_recording
        with pytest.raises(RuntimeError, match="already recording"):
            recorder.start(lambda at, event: None)
    finally:
        recorder.stop()


def test_recorder_stops_cleanly_and_can_restart(xvfb):
    recorder = xi2.XI2Recorder(xvfb.name)
    events = []
    recorder.start(lambda at, event: events.append(event))
    recorder.stop()
    assert not recorder.is_recording
    recorder.start(lambda at, event: events.append(event))
    recorder.stop()


def test_key_events_are_captured_as_keydown_keyup(xvfb):
    """Grabs the keyboard before injecting, matching the scenario this feature is
    for: `capture_raw_input` only makes sense while a game already holds the
    device. Without a grab, XTEST's injection produces an *extra* raw event
    attributed to the master keyboard alongside the real one from its dedicated
    slave (`Virtual core XTEST keyboard`) - a real physical keyboard has no such
    echo, so this is an XTEST test-injection artifact, not something the
    recorder needs to defend against. See `backend/xi2.py`'s module docstring."""
    recorder = xi2.XI2Recorder(xvfb.name)
    events = []
    recorder.start(lambda at, event: events.append((at, event)))
    grabber = _connect(xvfb)
    try:
        status = grabber.screen().root.grab_keyboard(
            False, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime)
        assert status == 0, "setup: could not grab the keyboard at all"

        inject = _connect(xvfb)
        keycode = inject.keysym_to_keycode(ord("a"))
        xtest.fake_input(inject, X.KeyPress, keycode)
        inject.sync()
        xtest.fake_input(inject, X.KeyRelease, keycode)
        inject.sync()
        inject.close()
        assert _settle(events, 2) == [KeyDown("a"), KeyUp("a")]
    finally:
        grabber.ungrab_keyboard(X.CurrentTime)
        grabber.close()
        recorder.stop()


def test_button_and_scroll_events_are_captured(xvfb):
    """Grabbed for the same reason as the key test above: without a pointer grab,
    XTEST's injection duplicates onto the master pointer alongside its own
    slave device."""
    recorder = xi2.XI2Recorder(xvfb.name)
    events = []
    recorder.start(lambda at, event: events.append((at, event)))
    grabber = _connect(xvfb)
    try:
        root = grabber.screen().root
        status = root.grab_pointer(
            False, X.ButtonPressMask | X.ButtonReleaseMask, X.GrabModeAsync,
            X.GrabModeAsync, X.NONE, X.NONE, X.CurrentTime)
        assert status == 0, "setup: could not grab the pointer at all"

        inject = _connect(xvfb)
        xtest.fake_input(inject, X.ButtonPress, 1)  # left
        inject.sync()
        xtest.fake_input(inject, X.ButtonRelease, 1)
        inject.sync()
        xtest.fake_input(inject, X.ButtonPress, 4)  # scroll up
        inject.sync()
        xtest.fake_input(inject, X.ButtonRelease, 4)
        inject.sync()
        inject.close()

        observed = _settle(events, 3)
        assert observed == [MouseDown("left"), MouseUp("left"), Scroll("up", 1)], (
            "the scroll release must not stand for a second detent")
    finally:
        grabber.ungrab_pointer(X.CurrentTime)
        grabber.close()
        recorder.stop()


def test_relative_motion_is_captured_and_summed_exactly(xvfb):
    """The point of the whole backend: several raw deltas, individually captured,
    must sum to the exact intended displacement - the same property the M0
    load-bearing test proves against a grab and a warp, here against the
    recorder's own translation and fractional-carry bookkeeping. Grabbed for the
    same reason as the key and button tests above."""
    recorder = xi2.XI2Recorder(xvfb.name)
    events = []
    recorder.start(lambda at, event: events.append((at, event)))
    grabber = _connect(xvfb)
    try:
        root = grabber.screen().root
        status = root.grab_pointer(
            False, X.PointerMotionMask, X.GrabModeAsync, X.GrabModeAsync,
            X.NONE, X.NONE, X.CurrentTime)
        assert status == 0, "setup: could not grab the pointer at all"

        inject = _connect(xvfb)
        deltas = [(25, -10), (-8, 3), (14, 14), (-31, 2)]
        for dx, dy in deltas:
            xtest.fake_input(inject, X.MotionNotify, detail=1, x=dx, y=dy)
            inject.sync()
        inject.close()

        moves = [e for e in _settle(events, len(deltas)) if isinstance(e, MoveRel)]
        assert moves, "no MoveRel observed"
        assert sum(m.dx for m in moves) == sum(dx for dx, _ in deltas)
        assert sum(m.dy for m in moves) == sum(dy for _, dy in deltas)
    finally:
        grabber.ungrab_pointer(X.CurrentTime)
        grabber.close()
        recorder.stop()


# --- M2: RawHotkeyWatch, the panic stop's XI2 replacement ---------------------


def _wait_for(fired, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline and not fired:
        time.sleep(0.02)
    return fired


def _hardware_watch(xvfb):
    """A `RawHotkeyWatch` standing in for real hardware: `injected_ids=frozenset()`
    means nothing is filtered, since Xvfb has no way to produce a keypress that
    is not XTEST-synthesised. Real discovery would otherwise filter out every
    injection this test file uses to simulate a keypress at all - see
    `test_raw_hotkey_watch_does_not_self_trigger_on_a_replayed_panic_chord` for
    the counterpart test that exercises real discovery instead of bypassing it."""
    return xi2.RawHotkeyWatch(xvfb.name, injected_ids=frozenset())


def test_raw_hotkey_watch_fires_under_an_exclusive_keyboard_grab(xvfb):
    """The whole reason this class exists instead of HotkeyGrab in game mode:
    load-bearing, mirrors test_raw_key_press_survives_an_exclusive_keyboard_grab
    but through the watcher's own action-dispatch path."""
    watch = _hardware_watch(xvfb)
    grabber = _connect(xvfb)
    fired = []
    try:
        status = grabber.screen().root.grab_keyboard(
            False, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime)
        grabber.sync()
        assert status == 0, "setup: could not grab the keyboard at all"

        watch.start({"Escape": lambda: fired.append(True)})

        inject = _connect(xvfb)
        keycode = inject.keysym_to_keycode(XK.string_to_keysym("Escape"))
        xtest.fake_input(inject, X.KeyPress, keycode)
        inject.sync()
        inject.close()

        assert _wait_for(fired), "panic action did not fire under an exclusive grab"
    finally:
        grabber.ungrab_keyboard(X.CurrentTime)
        watch.stop()
        grabber.close()


def test_raw_hotkey_watch_does_not_consume_the_key(xvfb):
    """Accepted consequence from the plan: unlike HotkeyGrab, this is a passive
    subscription, so the key still reaches whatever window is grabbing/focused.
    Proven here by the grabbing client itself still observing the KeyPress it
    holds an exclusive grab for."""
    watch = _hardware_watch(xvfb)
    grabber = _connect(xvfb)
    fired = []
    try:
        status = grabber.screen().root.grab_keyboard(
            False, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime)
        grabber.sync()
        assert status == 0, "setup: could not grab the keyboard at all"

        watch.start({"Escape": lambda: fired.append(True)})

        inject = _connect(xvfb)
        keycode = inject.keysym_to_keycode(XK.string_to_keysym("Escape"))
        xtest.fake_input(inject, X.KeyPress, keycode)
        inject.sync()
        inject.close()

        assert _wait_for(fired), "setup: the watcher itself never fired"
        seen = _drain(grabber, lambda e: e.type == X.KeyPress, timeout=1.0)
        assert seen, "the grabbing client never saw the key: it was consumed"
    finally:
        grabber.ungrab_keyboard(X.CurrentTime)
        watch.stop()
        grabber.close()


def test_raw_hotkey_watch_reconstructs_a_modifier_chord(xvfb):
    """Gate 2 proved the raw stream carries no modifier field of its own; this is
    that tracking built as reusable code, matching X11 core events reporting
    Ctrl+Escape only when Escape is pressed while Ctrl is down."""
    watch = _hardware_watch(xvfb)
    fired = []
    try:
        watch.start({"Ctrl+Escape": lambda: fired.append(True)})

        inject = _connect(xvfb)
        escape = inject.keysym_to_keycode(XK.string_to_keysym("Escape"))
        xtest.fake_input(inject, X.KeyPress, escape)
        inject.sync()
        xtest.fake_input(inject, X.KeyRelease, escape)
        inject.sync()
        assert not _wait_for(fired, timeout=0.5), (
            "bare Escape must not trigger a Ctrl+Escape panic stop")

        ctrl = inject.keysym_to_keycode(XK.string_to_keysym("Control_L"))
        xtest.fake_input(inject, X.KeyPress, ctrl)
        inject.sync()
        xtest.fake_input(inject, X.KeyPress, escape)
        inject.sync()
        inject.close()

        assert _wait_for(fired), "Ctrl+Escape did not fire"
    finally:
        watch.stop()


def test_raw_hotkey_watch_seeds_already_held_modifiers(xvfb):
    """A modifier held before playback starts - the user still has Ctrl down from
    whatever they were doing - must count from the watcher's first event, not
    only from a press it happens to see afterwards. query_keymap() at start()
    is what makes that true."""
    watch = _hardware_watch(xvfb)
    inject = _connect(xvfb)
    fired = []
    try:
        ctrl = inject.keysym_to_keycode(XK.string_to_keysym("Control_L"))
        xtest.fake_input(inject, X.KeyPress, ctrl)
        inject.sync()

        watch.start({"Ctrl+Escape": lambda: fired.append(True)})

        escape = inject.keysym_to_keycode(XK.string_to_keysym("Escape"))
        xtest.fake_input(inject, X.KeyPress, escape)
        inject.sync()

        assert _wait_for(fired), "a modifier already held at start() was not seeded"
    finally:
        xtest.fake_input(inject, X.KeyRelease, ctrl)
        inject.sync()
        inject.close()
        watch.stop()


def test_raw_hotkey_watch_ignores_lock_modifiers(xvfb):
    """CapsLock must never decide whether a hotkey matches, the same rule
    MODIFIER_BITS/LOCK_MASKS encode for HotkeyGrab. Holding Caps_Lock down must
    not turn a bare Escape binding into a chord that never fires."""
    watch = _hardware_watch(xvfb)
    inject = _connect(xvfb)
    fired = []
    try:
        caps = inject.keysym_to_keycode(XK.string_to_keysym("Caps_Lock"))
        xtest.fake_input(inject, X.KeyPress, caps)
        inject.sync()

        watch.start({"Escape": lambda: fired.append(True)})

        escape = inject.keysym_to_keycode(XK.string_to_keysym("Escape"))
        xtest.fake_input(inject, X.KeyPress, escape)
        inject.sync()

        assert _wait_for(fired), "Caps_Lock being held must not suppress Escape"
    finally:
        xtest.fake_input(inject, X.KeyRelease, caps)
        inject.sync()
        inject.close()
        watch.stop()


def test_raw_hotkey_watch_rejects_a_duplicate_chord(xvfb):
    watch = xi2.RawHotkeyWatch(xvfb.name)
    with pytest.raises(GrabUnavailable, match="same combination"):
        watch.start({"Escape": lambda: None, "esc": lambda: None})
    assert not watch.is_active


def test_raw_hotkey_watch_stops_cleanly_and_can_restart(xvfb):
    watch = xi2.RawHotkeyWatch(xvfb.name)
    watch.start({"Escape": lambda: None})
    assert watch.is_active
    watch.stop()
    assert not watch.is_active
    assert watch.grabbed == []
    watch.start({"Escape": lambda: None})
    assert watch.grabbed == ["Escape"]
    watch.stop()


def test_raw_hotkey_watch_does_not_self_trigger_on_a_replayed_panic_chord(xvfb):
    """The hazard this whole plan exists to close, in its own words: without
    sourceid filtering, a macro genuinely containing the exact modified panic
    chord (Ctrl+Escape as ordinary content, not the panic key itself) still
    stopped itself when replayed, because XTEST-injected keys were visible on
    the raw stream the same as real ones. This is the inverse of the old
    `..._self_triggers` test this replaces, now exercising real discovery
    (no `injected_ids` override) rather than `_hardware_watch`'s bypass."""
    watch = xi2.RawHotkeyWatch(xvfb.name)
    inject = _connect(xvfb)
    fired = []
    try:
        watch.start({"Ctrl+Escape": lambda: fired.append(True)})

        ctrl = inject.keysym_to_keycode(XK.string_to_keysym("Control_L"))
        escape = inject.keysym_to_keycode(XK.string_to_keysym("Escape"))
        xtest.fake_input(inject, X.KeyPress, ctrl)
        inject.sync()
        xtest.fake_input(inject, X.KeyPress, escape)
        inject.sync()
        xtest.fake_input(inject, X.KeyRelease, escape)
        inject.sync()
        xtest.fake_input(inject, X.KeyRelease, ctrl)
        inject.sync()

        assert not _wait_for(fired, timeout=0.5), (
            "a replayed Ctrl+Escape must not stop the macro that contains it")
    finally:
        inject.close()
        watch.stop()


def test_raw_hotkey_watch_fails_closed_when_discovery_raises(xvfb, monkeypatch):
    """A discovery failure must not degrade to an unfiltered watch: with warn-
    and-skip gone in game mode (see AGENTS.md), an unfiltered watch would let a
    replayed panic key stop the macro that contains it, which is worse than no
    panic stop at all."""
    def boom(dpy):
        raise RuntimeError("simulated XIQueryDevice failure")
    monkeypatch.setattr(xi2, "xtest_device_ids", boom)

    watch = xi2.RawHotkeyWatch(xvfb.name)
    with pytest.raises(RuntimeError, match="simulated XIQueryDevice failure"):
        watch.start({"Escape": lambda: None})
    assert not watch.is_active


def test_raw_hotkey_watch_fails_closed_when_discovery_finds_nothing(xvfb, monkeypatch):
    """An empty discovery result is treated as broken discovery, not as 'nothing
    to filter' - some XTEST device always exists wherever XTEST itself does."""
    monkeypatch.setattr(xi2, "xtest_device_ids", lambda dpy: frozenset())

    watch = xi2.RawHotkeyWatch(xvfb.name)
    with pytest.raises(RuntimeError, match="no XTEST devices"):
        watch.start({"Escape": lambda: None})
    assert not watch.is_active


def test_raw_hotkey_watch_explicit_empty_injected_ids_is_not_a_failure(xvfb):
    """`_hardware_watch`'s bypass must keep working: an explicit `injected_ids`
    is used verbatim, even when empty, and does not trigger the discovery
    failure path above."""
    watch = _hardware_watch(xvfb)
    watch.start({"Escape": lambda: None})
    try:
        assert watch.is_active
    finally:
        watch.stop()
