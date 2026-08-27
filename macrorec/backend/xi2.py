"""XI2 raw input: sees through a game's pointer grab and warp-to-centre.

M0 spike. See `~/.claude/plans/linux-macrorec-plan-capturing-delegated-eich.md` for the
full design and the measurements this module is built from.

**Why this exists.** A fullscreen FPS grabs the pointer, reads a per-frame relative
delta, and warps the cursor back to screen centre every frame so it can never reach an
edge. `X11Recorder` taps core `MotionNotify` and stores absolute position, so the
user's motion and the game's warp-corrections land in the same stream and cancel to
net zero over a turn. XI2 `RawMotion` comes from the device before the server applies
a grab or a warp, so it sees the turn the core stream cannot.

**python-xlib 0.33 has no parser for raw events.** `Xlib.ext.xinput.init()` registers
`ge_add_event_data` only for the non-raw device events (ButtonPress, ButtonRelease,
KeyPress, KeyRelease, Motion, DeviceChanged, HierarchyChanged, PropertyEvent) -
`RawKeyPress`/`RawKeyRelease`/`RawButtonPress`/`RawButtonRelease`/`RawMotion` are
absent from that table, so a `GenericEvent`'s `.data` arrives as unparsed bytes.
`register()` below fills that gap for one display connection.

**The wire layout was measured, not taken from a header**, against this machine's
Xvfb and this python-xlib version, decoded against a known injected `(37, -19)`:
deviceid(Card16) time(Card32) detail(Card32) sourceid(Card16) valuators_len(Card16)
flags(Card32) pad(4) valuator_mask(valuators_len*4 bytes) axisvalues[popcount(mask)]
axisvalues_raw[popcount(mask)], the last two as FP3232 (signed int32 integral, then
uint32 fraction). `test_xi2_backend.py` asserts this layout directly rather than
trusting this comment. A `RawKeyPress`/`RawButtonPress` payload has no axes: the mask
is present but zero, and the payload ends right after it (30 bytes, not 62).

`detail` carries the keycode for `RawKeyPress`/`RawKeyRelease` and the button number
for `RawButtonPress`/`RawButtonRelease` - the same numbers `x11.py`'s
`keycode_to_sym` and `button_name` already translate, since raw events report the
same device-level numbers core events do.

**Raw events carry no modifier state.** Compare `xinput.DeviceEventData`, which has an
explicit `mods` field; the raw payload does not, because raw events are pre-transform
device data and modifiers are a server-side concept applied afterwards. A modifier
chord has to be reconstructed by tracking `RawKeyPress`/`RawKeyRelease` for known
modifier keycodes and maintaining that state across events - proven feasible for M0
(see the plan's Gate 2), not yet built here.

**A device-count wrinkle, measured 2026-08-22, is specific to XTEST, not real
hardware.** Injecting one relative move through XTEST with nothing grabbing the
pointer produces *two* `RawMotion` events: one from `Virtual core XTEST pointer`
(a slave device XTEST owns) and one attributed to the master `Virtual core pointer`
itself. `XIQueryDevice` shows why: XTEST synthesises input through its own
dedicated slave, which a real mouse has no equivalent of - a real device is a
single slave (see `Xvfb mouse` in the same device list) and reports once. Under an
active pointer grab the master-attributed echo did not appear in that same probe,
which is what the M0 load-bearing test's `len(devices) == 1` assertion checks - but
that grab correlation was observed once, in Xvfb, with XTEST as the motion source,
and is not asserted here as a general XI2 guarantee. `XI2Recorder` does not
special-case any device id.

**The master-attributed echo carries the XTEST slave's `sourceid`, not the
master's own - measured 2026-08-23.** Injecting one keystroke ungrabbed produced
two `RawKeyPress`/`RawKeyRelease` pairs, `deviceid=5` (`Virtual core XTEST
keyboard`) and `deviceid=3` (`Virtual core keyboard`, the master); both carried
`sourceid=5`. So a filter on `sourceid` alone catches both copies, not only the
one already attributed to the slave, which is what makes `xtest_device_ids()`
below safe to use ungrabbed and not just under a grab. Grabbed, only the single
`deviceid=5`/`sourceid=5` pair arrived, consistent with the motion finding above.
Every XTEST slave device (`Virtual core XTEST pointer` id 4, `Virtual core XTEST
keyboard` id 5 on this Xvfb) carries an `XTEST Device` property; that is what
`xtest_device_ids()` keys on.
"""

from __future__ import annotations

import select
import struct
import threading
import time
from dataclasses import dataclass

from Xlib import X, display
from Xlib.ext import xinput

from ..events import Event, KeyDown, KeyUp, MouseDown, MouseUp, MoveRel, Scroll
from .base import EventSink, Recorder
from .x11 import (
    MODIFIER_SYMS_BY_BIT,
    SCROLL_BUTTONS,
    GrabUnavailable,
    KeyResolutionError,
    button_name,
    format_hotkey,
    keycode_to_sym,
    parse_hotkey,
    resolve_key,
)

#: Every raw XI2 event type this module knows how to parse.
RAW_EVENT_TYPES = (
    xinput.RawKeyPress,
    xinput.RawKeyRelease,
    xinput.RawButtonPress,
    xinput.RawButtonRelease,
    xinput.RawMotion,
)

#: `RawMotion`'s mask bit 0 is the x axis, bit 1 is y - true for every mouse this
#: backend has been measured against. A device with more valuators (tablet, touchpad
#: gestures) would need its own axis map; out of scope for M0.
AXIS_X = 0
AXIS_Y = 1


@dataclass(frozen=True)
class RawEvent:
    """One parsed `RawKeyPress`/`RawKeyRelease`/`RawButtonPress`/`RawButtonRelease`/
    `RawMotion` payload.

    `detail` is the keycode (key events) or button number (button events); 0 for
    `RawMotion`, which has no detail of its own. `sourceid` names the device that
    actually produced the event - for an XTEST-synthesised key or button this is
    the XTEST slave's id even when `deviceid` reports the master, which is what
    lets `xtest_device_ids()` catch both copies described in the module
    docstring. `axisvalues` is accelerated, `axisvalues_raw` is unaccelerated -
    what a game reads, and therefore what replay should reproduce. Both are keyed
    by axis index via `mask`, not by position in the tuple:
    `axis(mask, axisvalues_raw, AXIS_X)` reads the right one even if a device
    omits an axis.
    """

    deviceid: int
    time: int
    detail: int
    sourceid: int
    mask: int
    axisvalues: tuple[float, ...]
    axisvalues_raw: tuple[float, ...]


def axis(mask: int, values: tuple[float, ...], index: int) -> float | None:
    """The value for axis `index`, or None if that axis is absent from `mask`.

    `values` holds one entry per *set* bit, in ascending bit order, not one entry
    per possible axis - `mask=0b10` (`AXIS_Y` only) puts y's value at `values[0]`.
    """
    if not (mask >> index) & 1:
        return None
    position = bin(mask & ((1 << index) - 1)).count("1")
    return values[position]


def _read_fp3232(data: bytes, offset: int, count: int) -> tuple[float, ...]:
    values = []
    position = offset
    for _ in range(count):
        integral, frac = struct.unpack_from("<iI", data, position)
        values.append(integral + frac / (1 << 32))
        position += 8
    return tuple(values)


class _RawEventData:
    """The `ge_add_event_data` handler for the five raw event types.

    Not an `Xlib.protocol.rq.Struct`: a struct's variable-length fields must have
    their count carried by a preceding integer field (`rq.LengthOf`), and the axis
    arrays here are sized by `popcount(mask)`, a value no such field expresses.
    `ClassInfoClass` in `Xlib.ext.xinput` sets the precedent for a plain object with
    a `parse_binary(data, display) -> (value, remaining_data)` method instead.
    """

    structcode = None

    @staticmethod
    def parse_binary(data: bytes, display) -> tuple[RawEvent, bytes]:
        deviceid, time_ = struct.unpack_from("<HL", data, 0)
        detail, = struct.unpack_from("<L", data, 6)
        sourceid, = struct.unpack_from("<H", data, 10)
        valuators_len, = struct.unpack_from("<H", data, 12)
        mask_len = valuators_len * 4
        mask_bytes = data[22:22 + mask_len]
        mask = int.from_bytes(mask_bytes, "little") if mask_bytes else 0
        axis_count = bin(mask).count("1")

        offset = 22 + mask_len
        axisvalues = _read_fp3232(data, offset, axis_count)
        offset += axis_count * 8
        axisvalues_raw = _read_fp3232(data, offset, axis_count)
        offset += axis_count * 8

        event = RawEvent(
            deviceid, time_, detail, sourceid, mask, axisvalues, axisvalues_raw)
        return event, data[offset:]


def register(dpy) -> None:
    """Teach one display connection to parse raw XI2 events.

    Per-connection: `ge_event_data` lives on the protocol-level display object
    behind `dpy`, so a second `Display()` needs its own call. Idempotent - calling
    it twice just overwrites the same five dict entries with the same value.
    """
    major = dpy.display.get_extension_major("XInputExtension")
    for evtype in RAW_EVENT_TYPES:
        dpy.ge_add_event_data(major, evtype, _RawEventData)


def select_raw_events(dpy, mask: int, window=None) -> None:
    """Subscribe `window` (default: the root window) to raw events from every
    device. `mask` is the OR of the `xinput.Raw*Mask` constants wanted, and is
    required rather than defaulted: a mask of 0 subscribes to nothing and
    returns without error, which would otherwise read as "no events happened"
    instead of "the caller forgot the mask". A caller after only motion should
    pass just `xinput.RawMotionMask`, since a passive watcher otherwise pays
    for events it discards.

    XI2 requires `XIQueryVersion` before any other XI2 request, or the server
    answers `BadRequest` - easy to miss because the Xvfb probes this module was
    built against tolerated skipping it.
    """
    dpy.xinput_query_version()
    target = window if window is not None else dpy.screen().root
    target.xinput_select_events([(xinput.AllDevices, mask)])
    dpy.sync()


#: The name a raw event's `sourceid` is compared against once its own
#: `XTEST Device` property lookup fails - measured on this machine's Xvfb, where
#: both slaves XTEST owns are named "Virtual core XTEST pointer"/"...keyboard".
_XTEST_NAME_HINT = "XTEST"


def xtest_device_ids(dpy) -> frozenset[int]:
    """Every device id XTEST synthesises input through, identified by its
    `XTEST Device` property (what `xinput(1)` itself keys on), falling back to
    a name match if that property is absent from every device on this server.

    Does not catch and does not default to an empty result on failure: a caller
    filtering on this set to keep a panic key from self-triggering must be able
    to tell "found nothing to filter" from "discovery is broken" and refuse to
    proceed in the latter case rather than run unfiltered. See
    `RawHotkeyWatch.start()`.
    """
    dpy.xinput_query_version()
    devices = dpy.xinput_query_device(xinput.AllDevices).devices

    by_property = set()
    for device in devices:
        atoms = dpy.xinput_list_device_properties(device.deviceid).atoms
        names = {dpy.get_atom_name(atom) for atom in atoms}
        if "XTEST Device" in names:
            by_property.add(device.deviceid)
    if by_property:
        return frozenset(by_property)

    return frozenset(
        device.deviceid for device in devices
        if _XTEST_NAME_HINT in device.name)


_RAW_MASK = (
    xinput.RawKeyPressMask | xinput.RawKeyReleaseMask
    | xinput.RawButtonPressMask | xinput.RawButtonReleaseMask
    | xinput.RawMotionMask
)


class XI2Recorder(Recorder):
    """Captures input via XI2 raw events - key, button and motion all from one
    connection. In game mode this replaces `X11Recorder` entirely rather than
    running alongside it: a game's warp-to-centre still generates core
    `MotionNotify`, which `X11Recorder` would capture as junk `move` lines, and
    merging two streams would need cross-stream timestamp sorting for no benefit.

    Same three-method `Recorder` contract as `X11Recorder`. Readiness is a real
    round trip, not a guessed sleep: `select_raw_events`'s `sync()` after
    `xinput_select_events` blocks until the server has acknowledged the
    selection, so it is provably active once `start()` returns - there is no
    `StartOfData`-style asynchronous reply to wait for here, XI2 selection is a
    synchronous request.
    """

    def __init__(self, display_name: str | None = None):
        self._display_name = display_name
        self._display = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._recording = False
        self._origin = 0.0
        #: Fractional remainder carried between raw samples, per axis. XI2 axis
        #: values are FP3232 (fractional in general) but `MoveRel` is integer, so
        #: rounding each sample independently would accumulate error over a long
        #: turn - hundreds of samples each losing a fraction of a pixel adds up to
        #: real distance lost. Carrying the remainder forward means the *sum* of
        #: everything emitted is exact even though no single sample is.
        self._carry_x = 0.0
        self._carry_y = 0.0

    def start(self, sink: EventSink) -> None:
        if self._recording:
            raise RuntimeError("already recording")

        self._display = display.Display(self._display_name)
        if not self._display.has_extension("XInputExtension"):
            self._display.close()
            self._display = None
            raise RuntimeError("this X server has no XInputExtension")

        register(self._display)
        select_raw_events(self._display, _RAW_MASK)

        self._carry_x = 0.0
        self._carry_y = 0.0
        self._recording = True
        self._origin = time.monotonic()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._capture, args=(sink,), daemon=True)
        self._thread.start()

    def _capture(self, sink: EventSink) -> None:
        fileno = self._display.fileno()
        while not self._stop_event.is_set():
            readable, _, _ = select.select([fileno], [], [], 0.1)
            if not readable:
                continue
            for _ in range(self._display.pending_events()):
                raw = self._display.next_event()
                translated = self._translate(raw)
                if translated is not None:
                    sink(time.monotonic() - self._origin, translated)

    def _translate(self, raw) -> Event | None:
        evtype = getattr(raw, "evtype", None)
        payload = getattr(raw, "data", None)
        if not isinstance(payload, RawEvent):
            return None

        if evtype == xinput.RawKeyPress:
            return KeyDown(keycode_to_sym(self._display, payload.detail))
        if evtype == xinput.RawKeyRelease:
            return KeyUp(keycode_to_sym(self._display, payload.detail))
        if evtype == xinput.RawButtonPress:
            if payload.detail in SCROLL_BUTTONS:
                return Scroll(SCROLL_BUTTONS[payload.detail], 1)
            return MouseDown(button_name(payload.detail))
        if evtype == xinput.RawButtonRelease:
            if payload.detail in SCROLL_BUTTONS:
                return None  # the press already stood for the whole detent
            return MouseUp(button_name(payload.detail))
        if evtype == xinput.RawMotion:
            return self._translate_motion(payload)
        return None

    def _translate_motion(self, payload: RawEvent) -> MoveRel:
        """Always returns an event, even `MoveRel(0, 0)`.

        A sub-half-unit sample can round away to nothing, but the *sample* still
        happened at a real instant. Returning None there would drop it from the
        sink entirely, and `accumulate_motion`/`to_events` only see timestamps
        for events that arrive - a slow drag made of many near-zero samples
        would lose the time they took, not just their (already-preserved, via
        `_carry_x`/`_carry_y`) displacement. Emitting the zero keeps the sample's
        place in time; `accumulate_motion` sums it harmlessly.
        """
        dx = axis(payload.mask, payload.axisvalues_raw, AXIS_X) or 0.0
        dy = axis(payload.mask, payload.axisvalues_raw, AXIS_Y) or 0.0

        self._carry_x += dx
        self._carry_y += dy
        emit_x = round(self._carry_x)
        emit_y = round(self._carry_y)
        self._carry_x -= emit_x
        self._carry_y -= emit_y

        return MoveRel(emit_x, emit_y)

    def stop(self) -> None:
        if not self._recording:
            return
        self._recording = False
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(2.0)
            self._thread = None
        if self._display is not None:
            try:
                self._display.close()
            except Exception:  # pragma: no cover
                pass
            self._display = None

    @property
    def is_recording(self) -> bool:
        return self._recording


_RAW_KEY_MASK = xinput.RawKeyPressMask | xinput.RawKeyReleaseMask


class RawHotkeyWatch:
    """A passive XI2 watcher standing in for `HotkeyGrab` on the panic key while
    a game holds an exclusive keyboard grab.

    `XGrabKey` cannot help there: a passive grab is itself excluded from a
    keyboard already grabbed by another client, which is exactly the case in a
    fullscreen game reading mouselook. `RawKeyPress` sees through that grab
    (proven in `test_raw_key_press_survives_an_exclusive_keyboard_grab`, the
    M0 go/no-go gate this class exists because of).

    **Accepted consequence, not a bug**: `xinput_select_events` is passive
    observation, not a grab, so the watched key is never withheld from the
    window that actually has focus. Where an `Escape` panic stop under
    `HotkeyGrab` reaches nobody else, here it also opens the game's own menu.
    Tolerable because playback is already being aborted; see AGENTS.md.

    **Ignores its own injected keys, by `sourceid`.** `X11Player` injects with
    XTEST, and this watcher used to see exactly those events, same as anything
    else on the raw stream -
    `test_raw_hotkey_watch_does_not_self_trigger_on_a_replayed_panic_chord`
    covers the hazard this closes. `injected_ids` names the device ids to
    ignore; `None` (the default) discovers them fresh in `start()` via
    `xtest_device_ids()`. Discovery failing, or finding nothing, is refused
    rather than tolerated: `AGENTS.md` explains why an unfiltered watch is
    worse than no watch at all once the panic key is genuinely being injected.

    Same shape as `HotkeyGrab` (`start`/`stop`/`is_active`/`grabbed`) so
    `gui.py` can hold either behind one reference. A chord fires the moment
    its own key goes down, same as `HotkeyGrab`.
    """

    def __init__(self, display_name: str | None = None,
                 injected_ids: frozenset[int] | None = None):
        self._display_name = display_name
        self._display = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        #: (keycode, modifier state) -> action, matching HotkeyGrab's lookup.
        self._bound: dict[tuple[int, int], object] = {}
        self._specs: list[str] = []
        #: keycode -> the MODIFIER_BITS bit it stands for, both sides of the
        #: keyboard included. Deliberately excludes CapsLock/NumLock, the same
        #: way MODIFIER_SYMS_BY_BIT does - lock state must never decide whether
        #: a hotkey matches.
        self._modifier_bit_by_keycode: dict[int, int] = {}
        #: Which of those keycodes are currently down, tracked from raw
        #: press/release since the payload carries no modifier field of its
        #: own (unlike core events' `state`).
        self._held_keycodes: set[int] = set()
        #: An explicit override (the test seam standing in for real hardware,
        #: where nothing should be filtered); None means "discover in start()".
        self._injected_ids_arg = injected_ids
        #: The set actually applied by `_watch()` - resolved fresh every
        #: `start()`, since the constructor's `None` means discover, not
        #: "nothing is injected".
        self._injected_ids: frozenset[int] = frozenset()

    def start(self, bindings) -> None:
        """Same contract as `HotkeyGrab.start`: `bindings` maps a hotkey spec
        to a callable, empty specs are skipped, and an empty result costs
        nothing."""
        if self._thread is not None:
            raise RuntimeError("hotkey watch already active")
        wanted = {spec: action for spec, action in bindings.items() if spec}
        if not wanted:
            return

        self._display = display.Display(self._display_name)
        if not self._display.has_extension("XInputExtension"):
            self._display.close()
            self._display = None
            raise RuntimeError("this X server has no XInputExtension")
        register(self._display)

        if self._injected_ids_arg is not None:
            self._injected_ids = self._injected_ids_arg
        else:
            try:
                discovered = xtest_device_ids(self._display)
            except Exception as exc:
                self._display.close()
                self._display = None
                raise RuntimeError(
                    f"could not tell injected keys from real ones: {exc}") from exc
            if not discovered:
                self._display.close()
                self._display = None
                raise RuntimeError(
                    "found no XTEST devices to filter; refusing to arm an "
                    "unfiltered panic watch")
            self._injected_ids = discovered

        for bit, syms in MODIFIER_SYMS_BY_BIT.items():
            for sym in syms:
                try:
                    keycode, _ = resolve_key(self._display, sym)
                except KeyResolutionError:
                    continue
                self._modifier_bit_by_keycode[keycode] = bit

        seen: dict[tuple[int, int], str] = {}
        for spec, action in wanted.items():
            mask, sym = parse_hotkey(spec)
            keycode, level = resolve_key(self._display, sym)
            # Punctuation that needs Shift to type at all gets it supplied,
            # matching HotkeyGrab; letters never do, see parse_hotkey.
            if level == 1:
                mask |= X.ShiftMask

            chord = (keycode, mask)
            if chord in seen:
                self.stop()
                raise GrabUnavailable(
                    f"{spec!r} and {seen[chord]!r} are the same combination")
            seen[chord] = spec
            self._bound[chord] = action
            self._specs.append(format_hotkey(mask, sym))

        # A modifier already held when playback starts - Ctrl still down from
        # whatever the user was doing a moment ago - must count from the first
        # event, not just from the next press this watcher happens to see, or
        # the tracked state is wrong for the rest of the run.
        keymap = self._display.query_keymap()
        for keycode in self._modifier_bit_by_keycode:
            if keymap[keycode // 8] & (1 << (keycode % 8)):
                self._held_keycodes.add(keycode)

        select_raw_events(self._display, _RAW_KEY_MASK)

        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def _current_state(self) -> int:
        state = 0
        for keycode in self._held_keycodes:
            state |= self._modifier_bit_by_keycode.get(keycode, 0)
        return state

    def _watch(self) -> None:
        fileno = self._display.fileno()
        while not self._stop.is_set():
            readable, _, _ = select.select([fileno], [], [], 0.1)
            if not readable:
                continue
            for _ in range(self._display.pending_events()):
                raw = self._display.next_event()
                evtype = getattr(raw, "evtype", None)
                payload = getattr(raw, "data", None)
                if not isinstance(payload, RawEvent):
                    continue
                if payload.sourceid in self._injected_ids:
                    # Our own XTEST injection, not a real keypress - dropped
                    # before it can enter held-modifier state or fire an
                    # action, so an injected Ctrl can never combine with a
                    # real key to form a chord either.
                    continue
                if evtype == xinput.RawKeyPress:
                    if payload.detail in self._modifier_bit_by_keycode:
                        self._held_keycodes.add(payload.detail)
                    action = self._bound.get((payload.detail, self._current_state()))
                    if action is not None:
                        action()
                elif evtype == xinput.RawKeyRelease:
                    self._held_keycodes.discard(payload.detail)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)
            self._thread = None
        if self._display is not None:
            try:
                self._display.close()
            except Exception:  # pragma: no cover
                pass
            self._display = None
        self._bound = {}
        self._specs = []
        self._modifier_bit_by_keycode = {}
        self._held_keycodes = set()
        self._injected_ids = frozenset()

    @property
    def is_active(self) -> bool:
        return self._thread is not None

    @property
    def grabbed(self) -> list[str]:
        return sorted(self._specs)
