"""Integration tests for the X backend, against a headless Xvfb.

Assertions are on what XRecord observed, never on a client window receiving input:
Xvfb has no window manager and no mapped client, so XTEST events reach no focus
window. XRecord taps the stream server-side and sees them regardless.
"""

from __future__ import annotations

import threading
import time

import pytest

from macrorec.collapse import collapse_motion
from macrorec.events import (
    Click,
    KeyDown,
    KeyTap,
    KeyUp,
    Macro,
    MouseDown,
    MouseUp,
    Move,
    Scroll,
    TypeText,
)
from macrorec.script import format_macro, parse
from macrorec.timeline import build_schedule, to_events

x11 = pytest.importorskip("macrorec.backend.x11")


def settle(events, expected, timeout=3.0):
    """Wait for the recorder thread to deliver `expected` events, then return them."""
    deadline = time.time() + timeout
    while time.time() < deadline and len(events) < expected:
        time.sleep(0.02)
    return [event for _, event in events]


def only_keys(events):
    return [e for e in events if isinstance(e, (KeyDown, KeyUp))]


# --- keysym resolution -------------------------------------------------------


def test_resolve_key_reports_the_shift_level(dpy):
    lower_code, lower_level = x11.resolve_key(dpy, "a")
    upper_code, upper_level = x11.resolve_key(dpy, "A")
    assert lower_code == upper_code, "same physical key"
    assert lower_level == 0 and upper_level == 1, "only the level differs"


def test_resolve_key_rejects_names_the_keyboard_cannot_produce(dpy):
    with pytest.raises(x11.KeyResolutionError, match="unknown key name"):
        x11.resolve_key(dpy, "NotAKeyAtAll")


def test_keysym_name_round_trips(dpy):
    for name in ("a", "A", "Return", "Control_L", "space", "exclam"):
        assert x11.keysym_name(x11.keysym_for_name(name)) == name


def test_the_xkb_keysym_group_is_loaded():
    """Without it, ISO_Level3_Shift is unknown and every AltGr symbol resolves to a
    level whose modifier cannot be found. That is the level-2 form of the `hi1` bug,
    so it is worth an assertion rather than a silent import-time try/except."""
    assert x11.keysym_for_name("ISO_Level3_Shift") != 0


def test_a_level_needing_an_absent_modifier_raises(dpy, monkeypatch):
    monkeypatch.setattr(x11, "_LEVEL_MODIFIERS", dict(x11._LEVEL_MODIFIERS))
    x11._LEVEL_MODIFIERS[1] = ("No_Such_Modifier",)
    with pytest.raises(x11.KeyResolutionError, match="no No_Such_Modifier key"):
        x11.modifier_keycodes(dpy, 1)


def test_macro_warnings_covers_an_unreachable_modifier(dpy, monkeypatch):
    monkeypatch.setattr(x11, "_LEVEL_MODIFIERS", dict(x11._LEVEL_MODIFIERS))
    x11._LEVEL_MODIFIERS[1] = ("No_Such_Modifier",)
    warnings = x11.macro_warnings(parse("key A\n"), dpy)
    assert any("cannot produce" in w and "A" in w for w in warnings)


def test_keysym_name_falls_back_to_a_parseable_hex_form():
    name = x11.keysym_name(0x0FFFFF)
    assert name == "0xfffff"
    assert x11.keysym_for_name(name) == 0x0FFFFF
    assert parse(f"key {name}\n").events == [KeyTap(name)]


# --- injection ---------------------------------------------------------------


def test_key_tap_is_observed(xvfb, capture):
    _, events = capture
    player = x11.X11Player(skip_syms=())
    player.perform(KeyTap("a"))
    player.close()
    assert only_keys(settle(events, 2)) == [KeyDown("a"), KeyUp("a")]


def test_shifted_symbols_hold_shift_around_the_key(xvfb, capture):
    """The regression that matters: resolving to a keycode without a level would
    make this emit `h`, `i`, `1` with no error at all."""
    _, events = capture
    player = x11.X11Player()
    player.perform(TypeText("Hi!"))
    player.close()

    assert only_keys(settle(events, 10)) == [
        KeyDown("Shift_L"), KeyDown("h"), KeyUp("h"), KeyUp("Shift_L"),
        KeyDown("i"), KeyUp("i"),
        KeyDown("Shift_L"), KeyDown("1"), KeyUp("1"), KeyUp("Shift_L"),
    ]


def test_unshifted_text_holds_no_modifier(xvfb, capture):
    _, events = capture
    player = x11.X11Player()
    player.perform(TypeText("hi"))
    player.close()
    keys = only_keys(settle(events, 4))
    assert keys == [KeyDown("h"), KeyUp("h"), KeyDown("i"), KeyUp("i")]
    assert not any(e.sym.startswith("Shift") for e in keys)


def test_an_explicitly_held_modifier_is_not_released_underneath_the_macro(
        xvfb, capture):
    """`keydown shift` then `key A` must not have the auto-shift release the user's
    own Shift when the tap ends."""
    _, events = capture
    player = x11.X11Player()
    player.perform(KeyDown("Shift_L"))
    player.perform(KeyTap("A"))
    player.perform(KeyUp("Shift_L"))
    player.close()

    assert only_keys(settle(events, 4)) == [
        KeyDown("Shift_L"), KeyDown("a"), KeyUp("a"), KeyUp("Shift_L"),
    ]


def test_pointer_and_buttons_are_observed(xvfb, capture):
    _, events = capture
    player = x11.X11Player()
    player.perform(Move(300, 200))
    player.perform(Click("left"))
    player.perform(MouseDown("right"))
    player.perform(MouseUp("right"))
    player.close()

    observed = settle(events, 5)
    assert Move(300, 200) in observed
    assert [e for e in observed if not isinstance(e, Move)] == [
        MouseDown("left"), MouseUp("left"), MouseDown("right"), MouseUp("right"),
    ]


def test_scroll_records_as_one_event_per_detent(xvfb, capture):
    _, events = capture
    player = x11.X11Player()
    player.perform(Scroll("down", 3))
    player.close()

    observed = [e for e in settle(events, 3) if isinstance(e, Scroll)]
    assert observed == [Scroll("down", 1)] * 3, "release is not a second detent"


def test_skip_syms_suppresses_the_panic_key(xvfb, capture):
    _, events = capture
    player = x11.X11Player(skip_syms={"Escape"})
    player.perform(KeyTap("Escape"))
    player.perform(KeyTap("a"))
    player.close()

    assert only_keys(settle(events, 2)) == [KeyDown("a"), KeyUp("a")]
    assert player.skipped == ["Escape", "Escape"]


# --- recorder ----------------------------------------------------------------


def test_recorder_refuses_to_start_twice(xvfb, capture):
    recorder, _ = capture
    assert recorder.is_recording
    with pytest.raises(RuntimeError, match="already recording"):
        recorder.start(lambda at, event: None)


def test_recorder_stops_cleanly_and_can_restart(xvfb):
    recorder = x11.X11Recorder(xvfb.name)
    events = []
    recorder.start(lambda at, event: events.append(event))
    recorder.stop()
    assert not recorder.is_recording

    recorder.start(lambda at, event: events.append(event))
    player = x11.X11Player()
    player.perform(KeyTap("b"))
    player.close()
    deadline = time.time() + 3
    while time.time() < deadline and len(events) < 2:
        time.sleep(0.02)
    recorder.stop()
    assert KeyDown("b") in events


def test_recorded_timestamps_advance(xvfb, capture):
    _, events = capture
    player = x11.X11Player()
    player.perform(KeyTap("a"))
    time.sleep(0.3)
    player.perform(KeyTap("b"))
    player.close()

    settle(events, 4)
    times = [at for at, _ in events]
    assert times == sorted(times)
    assert times[-1] - times[0] >= 0.25


# --- panic grab --------------------------------------------------------------


def test_parse_hotkey_handles_modifiers():
    from Xlib import X

    # A letter resolves to its lower-case keysym: upper case names the key, it does
    # not ask for Shift.
    assert x11.parse_hotkey("A") == (0, "a")
    assert x11.parse_hotkey("Ctrl+A") == (X.ControlMask, "a")
    assert x11.parse_hotkey("Ctrl+Shift+A") == (
        X.ControlMask | X.ShiftMask, "a")
    assert x11.parse_hotkey("Alt+F4") == (X.Mod1Mask, "F4")
    assert x11.parse_hotkey("Super+r") == (X.Mod4Mask, "r")
    assert x11.parse_hotkey("Ctrl+a") == x11.parse_hotkey("Ctrl+A")


def test_parse_hotkey_is_case_and_alias_insensitive():
    assert x11.parse_hotkey("ctrl+shift+a") == x11.parse_hotkey("Control+SHIFT+a")
    assert x11.parse_hotkey("CTRL+esc") == x11.parse_hotkey("ctrl+Escape")
    assert x11.parse_hotkey("win+a") == x11.parse_hotkey("super+a")
    assert x11.parse_hotkey("  Ctrl + A  ") == x11.parse_hotkey("Ctrl+A")


def test_parse_hotkey_handles_the_plus_key_itself():
    """`Ctrl++` is Ctrl plus the plus key. A single trailing plus is an unfinished
    hotkey, not a request for that key, so the two must not be conflated."""
    from Xlib import X

    assert x11.parse_hotkey("Ctrl++") == (X.ControlMask, "plus")
    assert x11.parse_hotkey("+") == (0, "plus")
    with pytest.raises(x11.KeyResolutionError, match="no key"):
        x11.parse_hotkey("Ctrl+")


@pytest.mark.parametrize("spec, fragment", [
    ("", "empty"),
    ("   ", "empty"),
    ("Ctrl+", "no key"),
    ("Hyper+a", "unknown modifier"),
    ("Ctrl+Shft+A", "unknown modifier"),
])
def test_parse_hotkey_rejects_nonsense(spec, fragment):
    with pytest.raises(x11.KeyResolutionError, match=fragment):
        x11.parse_hotkey(spec)


def test_hotkeys_round_trip_to_a_canonical_spelling():
    # Letters display upper case, the conventional spelling, and Shift on a letter
    # is written out rather than implied by its case.
    assert x11.normalise_hotkey("shift+ctrl+a") == "Ctrl+Shift+A"
    assert x11.normalise_hotkey("ctrl+A") == "Ctrl+A"
    assert x11.normalise_hotkey("ctrl+a") == "Ctrl+A"
    assert x11.normalise_hotkey("CTRL+esc") == "Ctrl+Escape"
    assert x11.normalise_hotkey("F9") == "F9"
    # Canonical output must itself parse back unchanged.
    for spec in ("shift+ctrl+a", "alt+F4", "super+r", "Escape"):
        once = x11.normalise_hotkey(spec)
        assert x11.normalise_hotkey(once) == once


def test_only_an_unmodified_panic_key_is_withheld_from_playback():
    """A macro's plain Escape cannot trigger a Ctrl+Escape panic stop, so refusing
    to type Escape at all would break good macros for nothing."""
    assert x11.panic_skip_sym("Escape") == "Escape"
    assert x11.panic_skip_sym("Ctrl+Escape") is None
    assert x11.panic_skip_sym("Ctrl+Shift+A") is None
    assert x11.panic_skip_sym("nonsense+") is None


def test_a_modified_hotkey_fires_only_with_its_modifiers(xvfb):
    seen = []
    grab = x11.HotkeyGrab(xvfb.name)
    grab.start({"Ctrl+Shift+F9": lambda: seen.append("hit")})
    try:
        player = x11.X11Player()

        player.perform(KeyTap("F9"))  # bare, must not fire
        time.sleep(0.3)
        assert seen == [], "fired without its modifiers held"

        player.perform(KeyDown("Control_L"))
        player.perform(KeyTap("F9"))  # Ctrl only, still not enough
        player.perform(KeyUp("Control_L"))
        time.sleep(0.3)
        assert seen == [], "fired with only some of its modifiers"

        player.perform(KeyDown("Control_L"))
        player.perform(KeyDown("Shift_L"))
        player.perform(KeyTap("F9"))
        player.perform(KeyUp("Shift_L"))
        player.perform(KeyUp("Control_L"))
        player.close()

        deadline = time.time() + 3
        while time.time() < deadline and not seen:
            time.sleep(0.02)
        assert seen == ["hit"]
    finally:
        grab.stop()


def test_ctrl_plus_a_letter_does_not_secretly_require_shift(xvfb):
    """`Ctrl+A` means the A key with Ctrl, the way every toolkit spells it. Upper
    case names the key; it does not request Shift. Resolving `A` to its level-1
    keysym and adding ShiftMask would make this hotkey fire only on Ctrl+Shift+A,
    which is a different chord and leaves Ctrl+A dead."""
    fired = threading.Event()
    grab = x11.HotkeyGrab(xvfb.name)
    grab.start({"Ctrl+A": fired.set})
    try:
        player = x11.X11Player()
        player.perform(KeyDown("Control_L"))
        player.perform(KeyTap("a"))
        player.perform(KeyUp("Control_L"))
        player.close()
        assert fired.wait(3.0), "Ctrl+A did not fire on Ctrl+a"
    finally:
        grab.stop()


def test_shift_on_a_letter_must_be_written_out(xvfb):
    seen = []
    grab = x11.HotkeyGrab(xvfb.name)
    grab.start({"Ctrl+Shift+A": lambda: seen.append("hit")})
    try:
        player = x11.X11Player()
        player.perform(KeyDown("Control_L"))
        player.perform(KeyTap("a"))
        player.perform(KeyUp("Control_L"))
        time.sleep(0.3)
        assert seen == [], "Ctrl+Shift+A fired on plain Ctrl+a"

        player.perform(KeyDown("Control_L"))
        player.perform(KeyDown("Shift_L"))
        player.perform(KeyTap("a"))
        player.perform(KeyUp("Shift_L"))
        player.perform(KeyUp("Control_L"))
        player.close()
        deadline = time.time() + 3
        while time.time() < deadline and not seen:
            time.sleep(0.02)
        assert seen == ["hit"]
    finally:
        grab.stop()


def test_a_shifted_punctuation_hotkey_still_holds_shift_for_you(xvfb):
    """Unlike a letter, `+` genuinely needs Shift on a US layout, so the grab has
    to supply it. Otherwise the hotkey can never be typed."""
    fired = threading.Event()
    grab = x11.HotkeyGrab(xvfb.name)
    grab.start({"Ctrl++": fired.set})
    try:
        player = x11.X11Player()
        player.perform(KeyDown("Control_L"))
        player.perform(KeyTap("plus"))  # the player holds Shift to make a plus
        player.perform(KeyUp("Control_L"))
        player.close()
        assert fired.wait(3.0), "Ctrl++ did not fire"
    finally:
        grab.stop()


def test_two_specs_resolving_to_the_same_chord_are_rejected_clearly(xvfb):
    """`Ctrl+A` and `Ctrl+a` are one grab. Without a check, the second grab hits
    BadAccess from our own first one and blames 'another program'."""
    grab = x11.HotkeyGrab(xvfb.name)
    with pytest.raises(x11.GrabUnavailable, match="same"):
        grab.start({"Ctrl+A": lambda: None, "Ctrl+a": lambda: None})
    assert not grab.is_active
    grab.stop()


def test_hotkey_syms_lists_every_key_the_chord_involves():
    """Used to strip the stop-hotkey out of a recording, so it has to name the
    modifier keys too, not just the final one."""
    assert x11.hotkey_syms("F9") == {"F9"}
    assert x11.hotkey_syms("Ctrl+Shift+F9") == {
        "F9", "Control_L", "Control_R", "Shift_L", "Shift_R"}
    assert "Alt_L" in x11.hotkey_syms("Alt+F4")
    assert x11.hotkey_syms("nonsense+") == set()


def test_two_hotkeys_can_share_a_key_with_different_modifiers(xvfb):
    seen = []
    grab = x11.HotkeyGrab(xvfb.name)
    grab.start({
        "Ctrl+F9": lambda: seen.append("ctrl"),
        "Alt+F9": lambda: seen.append("alt"),
    })
    try:
        player = x11.X11Player()
        player.perform(KeyDown("Alt_L"))
        player.perform(KeyTap("F9"))
        player.perform(KeyUp("Alt_L"))
        player.perform(KeyDown("Control_L"))
        player.perform(KeyTap("F9"))
        player.perform(KeyUp("Control_L"))
        player.close()

        deadline = time.time() + 3
        while time.time() < deadline and len(seen) < 2:
            time.sleep(0.02)
        assert seen == ["alt", "ctrl"]
    finally:
        grab.stop()


def test_a_modified_hotkey_ignores_capslock_and_numlock(xvfb):
    """Lock state must never decide whether a hotkey matches."""
    from Xlib import X

    fired = threading.Event()
    grab = x11.HotkeyGrab(xvfb.name)
    grab.start({"Ctrl+F9": fired.set})
    try:
        assert (X.LockMask | X.Mod2Mask) & x11.MODIFIER_BITS == 0, (
            "lock bits must be excluded from hotkey matching")
        player = x11.X11Player()
        player.perform(KeyDown("Num_Lock"))
        player.perform(KeyDown("Control_L"))
        player.perform(KeyTap("F9"))
        player.perform(KeyUp("Control_L"))
        player.perform(KeyUp("Num_Lock"))
        player.close()
        assert fired.wait(3.0), "NumLock stopped the hotkey firing"
    finally:
        grab.stop()


def test_hotkey_grab_fires_on_the_grabbed_key(xvfb):
    fired = threading.Event()
    grab = x11.HotkeyGrab(xvfb.name)
    grab.start({"Escape": fired.set})
    try:
        assert grab.is_active
        player = x11.X11Player()
        player.perform(KeyTap("Escape"))
        player.close()
        assert fired.wait(3.0), "the grab never saw the key"
    finally:
        grab.stop()
    assert not grab.is_active


def test_hotkey_grab_ignores_other_keys(xvfb):
    fired = threading.Event()
    grab = x11.HotkeyGrab(xvfb.name)
    grab.start({"Escape": fired.set})
    try:
        player = x11.X11Player()
        player.perform(KeyTap("a"))
        player.close()
        assert not fired.wait(0.5)
    finally:
        grab.stop()


def test_hotkey_grab_routes_each_key_to_its_own_action(xvfb):
    seen = []
    grab = x11.HotkeyGrab(xvfb.name)
    grab.start({
        "F9": lambda: seen.append("record"),
        "F10": lambda: seen.append("play"),
    })
    try:
        player = x11.X11Player()
        player.perform(KeyTap("F10"))
        player.perform(KeyTap("F9"))
        player.close()
        deadline = time.time() + 3
        while time.time() < deadline and len(seen) < 2:
            time.sleep(0.02)
    finally:
        grab.stop()
    assert seen == ["play", "record"]


def test_an_unbound_hotkey_costs_nothing(xvfb):
    """Empty names are the default for Record and Play, and must not open a
    connection or take a key away from the desktop."""
    grab = x11.HotkeyGrab(xvfb.name)
    grab.start({"": lambda: None})
    assert not grab.is_active
    grab.stop()


def test_hotkey_grab_refuses_to_start_twice(xvfb):
    grab = x11.HotkeyGrab(xvfb.name)
    grab.start({"Escape": lambda: None})
    try:
        with pytest.raises(RuntimeError, match="already active"):
            grab.start({"Escape": lambda: None})
    finally:
        grab.stop()


def test_a_key_another_client_already_holds_is_reported(xvfb):
    """X reports a refused grab asynchronously, so `grab_key` never raises. Without
    an explicit error handler a dead hotkey looks exactly like a working one, which
    is precisely how the Escape panic stop silently did nothing."""
    from Xlib import X, XK, display

    other = display.Display(xvfb.name)
    keycode = other.keysym_to_keycode(XK.string_to_keysym("F8"))
    for mask in x11.LOCK_MASKS:
        other.screen().root.grab_key(keycode, mask, True,
                                     X.GrabModeAsync, X.GrabModeAsync)
    other.sync()
    try:
        grab = x11.HotkeyGrab(xvfb.name)
        with pytest.raises(x11.GrabUnavailable, match="F8"):
            grab.start({"F8": lambda: None})
        assert not grab.is_active
    finally:
        for mask in x11.LOCK_MASKS:
            other.screen().root.ungrab_key(keycode, mask)
        other.sync()
        other.close()


def test_the_panic_key_can_be_grabbed_under_a_real_window_manager(wm_display):
    """The regression that shipped. marco binds Alt+Escape, and the old code used
    `X.AnyModifier`, which the server then refuses wholesale with BadAccess. It
    failed silently, because X reports grab errors asynchronously.

    This runs against actual marco, not a stand-in for it.
    """
    from Xlib import X, XK, display, error

    probe = display.Display(wm_display.name)
    keycode = probe.keysym_to_keycode(XK.string_to_keysym("Escape"))
    caught = error.CatchError()
    probe.screen().root.grab_key(keycode, X.AnyModifier, True,
                                 X.GrabModeAsync, X.GrabModeAsync, onerror=caught)
    probe.sync()
    any_modifier_refused = caught.get_error() is not None
    probe.close()
    assert any_modifier_refused, (
        "marco is not holding Alt+Escape on this display, so this test is no "
        "longer exercising the real conflict")

    fired = threading.Event()
    grab = x11.HotkeyGrab(wm_display.name)
    grab.start({"Escape": fired.set})  # must not raise
    try:
        assert grab.is_active
        player = x11.X11Player(display.Display(wm_display.name))
        player.perform(KeyTap("Escape"))
        assert fired.wait(3.0), "the panic key did not fire under a real WM"
    finally:
        grab.stop()


def test_hotkeys_still_work_under_a_real_window_manager(wm_display):
    """marco grabs a pile of keys for its own bindings. A hotkey has to survive
    sharing a desktop with it."""
    from Xlib import display

    fired = threading.Event()
    grab = x11.HotkeyGrab(wm_display.name)
    grab.start({"Ctrl+Shift+F9": fired.set})
    try:
        player = x11.X11Player(display.Display(wm_display.name))
        player.perform(KeyDown("Control_L"))
        player.perform(KeyDown("Shift_L"))
        player.perform(KeyTap("F9"))
        player.perform(KeyUp("Shift_L"))
        player.perform(KeyUp("Control_L"))
        assert fired.wait(3.0), "Ctrl+Shift+F9 did not fire under a real WM"
    finally:
        grab.stop()


def test_a_modified_binding_elsewhere_does_not_block_the_plain_key(xvfb):
    """The actual MATE bug: it binds Alt+Escape to window switching, and an
    AnyModifier grab on Escape is then refused wholesale. Grabbing the explicit
    lock masks has to keep working."""
    from Xlib import X, XK, display

    other = display.Display(xvfb.name)
    keycode = other.keysym_to_keycode(XK.string_to_keysym("Escape"))
    other.screen().root.grab_key(keycode, X.Mod1Mask, True,
                                 X.GrabModeAsync, X.GrabModeAsync)
    other.sync()
    try:
        fired = threading.Event()
        grab = x11.HotkeyGrab(xvfb.name)
        grab.start({"Escape": fired.set})  # must not raise
        try:
            assert grab.is_active
            player = x11.X11Player()
            player.perform(KeyTap("Escape"))
            player.close()
            assert fired.wait(3.0), "Alt+Escape elsewhere killed our plain grab"
        finally:
            grab.stop()
    finally:
        other.screen().root.ungrab_key(keycode, X.Mod1Mask)
        other.sync()
        other.close()


# --- load-time warnings ------------------------------------------------------


def test_warns_when_a_macro_contains_the_panic_key(dpy):
    macro = parse("key Escape\nkey a\n")
    warnings = x11.macro_warnings(macro, dpy)
    assert any("panic key" in w and "skipped" in w for w in warnings)


@pytest.fixture
def layout_us(dpy):
    """Stamp a layout on the shared server, and take it back off again. The Xvfb
    fixture is session-scoped, so a test that left this behind would silently change
    what every later test sees."""
    from Xlib import Xatom

    atom = dpy.intern_atom("_XKB_RULES_NAMES")
    root = dpy.screen().root
    previous = root.get_full_property(atom, Xatom.STRING)
    root.change_property(atom, Xatom.STRING, 8, b"evdev\0pc105\0us\0\0\0")
    dpy.sync()
    try:
        yield "us"
    finally:
        if previous is None:
            root.delete_property(atom)
        else:
            root.change_property(atom, Xatom.STRING, 8, previous.value)
        dpy.sync()


def test_warns_about_a_layout_mismatch(dpy, layout_us):
    assert x11.current_layout(dpy) == layout_us
    assert not x11.macro_warnings(parse("layout us\nkey a\n"), dpy)
    warnings = x11.macro_warnings(parse("layout de\nkey a\n"), dpy)
    assert any("'de'" in w and "'us'" in w for w in warnings)


def test_a_macro_with_no_layout_header_never_warns_about_layout(dpy, layout_us):
    assert not any("layout" in w for w in x11.macro_warnings(parse("key a\n"), dpy))


def test_warns_about_keys_this_keyboard_cannot_produce(dpy):
    warnings = x11.macro_warnings(Macro(events=[KeyTap("Nonexistent_Key")]), dpy)
    assert any("cannot produce" in w for w in warnings)


def test_a_clean_macro_produces_no_warnings(dpy):
    assert x11.macro_warnings(parse('key a\ntype "hello"\nclick left\n'), dpy) == []


# --- the whole pipeline ------------------------------------------------------


def test_record_export_reimport_replay_round_trip(xvfb):
    """The real coverage: capture, reduce, write, re-read, schedule and replay,
    then assert the second capture matches the first."""
    recorder = x11.X11Recorder(xvfb.name)
    captured = []
    recorder.start(lambda at, event: captured.append((at, event)))

    player = x11.X11Player()
    player.perform(KeyTap("h"))
    player.perform(Move(120, 340))
    player.perform(Click("left"))
    player.perform(Scroll("down", 2))
    player.close()

    settle(captured, 9)
    recorder.stop()

    macro = Macro(
        events=collapse_motion(to_events(captured)),
        name="round-trip",
        layout=None,
    )
    text = format_macro(macro)
    reloaded = parse(text)
    assert reloaded == macro, "the file did not survive a write/read cycle"

    # Motion collapse must have kept the click position and dropped nothing else.
    assert Move(120, 340) in reloaded.events
    assert sum(isinstance(e, Move) for e in reloaded.events) == 1

    replay_recorder = x11.X11Recorder(xvfb.name)
    replayed = []
    replay_recorder.start(lambda at, event: replayed.append((at, event)))

    replay_player = x11.X11Player()
    for step in build_schedule(reloaded.events, speed=reloaded.speed):
        replay_player.perform(step.event)
    replay_player.close()

    settle(replayed, 9)
    replay_recorder.stop()

    first = [event for _, event in captured]
    second = [event for _, event in replayed]
    assert collapse_motion(second) == collapse_motion(first)
