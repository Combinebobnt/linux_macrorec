"""The X11 backend: XRecord to capture, XTEST to inject, XGrabKey to panic-stop.

All X-specific knowledge in the package lives here. Nothing above this module imports
Xlib, which is what keeps the parser, timeline and GUI testable without a display.
"""

from __future__ import annotations

import select
import threading
import time

from Xlib import X, XK, Xatom, display, error
from Xlib.ext import record, xtest
from Xlib.protocol import rq

from ..events import (
    Event,
    KeyDown,
    KeyTap,
    KeyUp,
    Move,
    MouseDown,
    MouseUp,
    Scroll,
    TypeText,
    expand_type,
    normalise_key,
)
from .base import EventSink, Player, Recorder

# ISO_Level3_Shift and friends are not in python-xlib's default keysym groups, and
# without them an AltGr symbol resolves to level 2 with no key to hold for it.
XK.load_keysym_group("xkb")

#: X button numbers 4-7 are wheel detents, not buttons anyone can press.
_SCROLL_BUTTONS = {4: "up", 5: "down", 6: "left", 7: "right"}
_BUTTON_NUMBERS = {"left": 1, "middle": 2, "right": 3}
_SCROLL_NUMBERS = {name: number for number, name in _SCROLL_BUTTONS.items()}

#: Keys that hold rather than type. Auto-shifting must not fight a macro that
#: holds one of these explicitly.
MODIFIER_SYMS = frozenset({
    "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
    "Meta_L", "Meta_R", "Super_L", "Super_R", "Hyper_L", "Hyper_R",
    "ISO_Level3_Shift", "Mode_switch", "Caps_Lock", "Num_Lock",
})

#: Which modifier each keymap level needs held. Level 0 needs nothing.
_LEVEL_MODIFIERS = {
    0: (),
    1: ("Shift_L",),
    2: ("ISO_Level3_Shift",),
    3: ("ISO_Level3_Shift", "Shift_L"),
}


def _build_keysym_names() -> dict[int, str]:
    names: dict[int, str] = {}
    for name, value in vars(XK).items():
        if name.startswith("XK_") and isinstance(value, int):
            names.setdefault(value, name[3:])
    return names


_KEYSYM_NAMES = _build_keysym_names()


class KeyResolutionError(LookupError):
    """A keysym name that this keyboard cannot produce."""


def keysym_name(keysym: int) -> str:
    """Name for a keysym number, falling back to a hex form the parser accepts."""
    return _KEYSYM_NAMES.get(keysym) or f"0x{keysym:04x}"


def keysym_for_name(name: str) -> int:
    """Keysym number for a name, accepting the hex fallback `keysym_name` emits."""
    if name.startswith("0x"):
        try:
            return int(name, 16)
        except ValueError:
            return 0
    return XK.string_to_keysym(name)


def resolve_key(dpy, sym: str) -> tuple[int, int]:
    """Keysym name to (keycode, level).

    Level matters as much as keycode: `A` and `a` share a keycode and differ only by
    the modifier held. Resolving to a keycode alone is how `type "Hi!"` silently
    becomes `hi1`.
    """
    keysym = keysym_for_name(sym)
    if not keysym:
        raise KeyResolutionError(f"unknown key name {sym!r}")
    candidates = [
        (keycode, level)
        for keycode, level in dpy.keysym_to_keycodes(keysym)
        if keycode and level in _LEVEL_MODIFIERS
    ]
    if not candidates:
        raise KeyResolutionError(
            f"{sym!r} is not on the current keyboard layout")
    # The lowest level needs the fewest modifiers held.
    return min(candidates, key=lambda pair: pair[1])


def modifier_keycodes(dpy, level: int) -> tuple[int, ...]:
    """Keycodes that must be held to reach `level`.

    Raises rather than skipping a modifier it cannot find. Dropping one silently
    would type the level-0 character instead, which is the exact failure resolving
    to a level exists to prevent.
    """
    out = []
    for name in _LEVEL_MODIFIERS[level]:
        try:
            keycode, _ = resolve_key(dpy, name)
        except KeyResolutionError:
            raise KeyResolutionError(
                f"this keyboard has no {name} key, so level {level} symbols "
                f"cannot be typed") from None
        out.append(keycode)
    return tuple(out)


def current_layout(dpy) -> str | None:
    """Layout name from the root window's `_XKB_RULES_NAMES`, or None.

    Recorded into the file header so replay can warn about a mismatch instead of
    silently typing the wrong characters. Read straight off the property because
    python-xlib ships no XKB extension module.
    """
    atom = dpy.intern_atom("_XKB_RULES_NAMES")
    prop = dpy.screen().root.get_full_property(atom, Xatom.STRING)
    if not prop or not prop.value:
        return None
    value = prop.value
    if isinstance(value, str):
        value = value.encode("latin-1", "replace")
    parts = bytes(value).split(b"\0")
    if len(parts) < 3 or not parts[2]:
        return None
    return parts[2].decode("latin-1")


class X11Player(Player):
    """Injects input with XTEST.

    `skip_syms` implements warn-and-skip for the panic key: playback drives some
    other application, so a macro that contained Escape would otherwise trip our own
    panic grab and stop itself.
    """

    def __init__(self, dpy=None, skip_syms=()):
        self.display = dpy if dpy is not None else display.Display()
        if not self.display.has_extension("XTEST"):
            raise RuntimeError("this X server has no XTEST extension")
        self.skip_syms = set(skip_syms)
        self.skipped: list[str] = []
        self._owns_display = dpy is None
        self._auto_modifiers: dict[str, tuple[int, ...]] = {}
        self._explicit_modifiers: set[int] = set()

    def key_down(self, sym: str) -> None:
        if sym in self.skip_syms:
            self.skipped.append(sym)
            return
        keycode, level = resolve_key(self.display, sym)
        modifiers = self._modifier_keycodes(level)
        for modifier in modifiers:
            xtest.fake_input(self.display, X.KeyPress, modifier)
        xtest.fake_input(self.display, X.KeyPress, keycode)
        self._auto_modifiers[sym] = modifiers
        if sym in MODIFIER_SYMS:
            self._explicit_modifiers.add(keycode)
        self.display.sync()

    def key_up(self, sym: str) -> None:
        if sym in self.skip_syms:
            self.skipped.append(sym)
            return
        keycode, _ = resolve_key(self.display, sym)
        xtest.fake_input(self.display, X.KeyRelease, keycode)
        for modifier in reversed(self._auto_modifiers.pop(sym, ())):
            xtest.fake_input(self.display, X.KeyRelease, modifier)
        self._explicit_modifiers.discard(keycode)
        self.display.sync()

    def move(self, x: int, y: int) -> None:
        xtest.fake_input(self.display, X.MotionNotify, x=x, y=y)
        self.display.sync()

    def button_down(self, button: str) -> None:
        xtest.fake_input(self.display, X.ButtonPress, _BUTTON_NUMBERS[button])
        self.display.sync()

    def button_up(self, button: str) -> None:
        xtest.fake_input(self.display, X.ButtonRelease, _BUTTON_NUMBERS[button])
        self.display.sync()

    def scroll(self, direction: str) -> None:
        number = _SCROLL_NUMBERS[direction]
        xtest.fake_input(self.display, X.ButtonPress, number)
        xtest.fake_input(self.display, X.ButtonRelease, number)
        self.display.sync()

    def close(self) -> None:
        if self._owns_display:
            self.display.close()

    def _modifier_keycodes(self, level: int) -> tuple[int, ...]:
        """Modifiers this level needs, minus any the macro is already holding."""
        return tuple(
            keycode for keycode in modifier_keycodes(self.display, level)
            if keycode not in self._explicit_modifiers
        )


class X11Recorder(Recorder):
    """Captures input with XRecord.

    Two connections and a thread, which is the shape the M0 spike settled on: the
    record connection blocks inside `record_enable_context()`, so the context can
    only be torn down from a second connection.

    Pointer motion is emitted raw. Reducing it is `collapse.collapse_motion`'s job,
    applied once recording stops.
    """

    def __init__(self, display_name: str | None = None):
        self._display_name = display_name
        self._record_display = None
        self._control_display = None
        self._context = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._live = False
        self._recording = False
        self._origin = 0.0

    def start(self, sink: EventSink) -> None:
        if self._recording:
            raise RuntimeError("already recording")

        self._record_display = display.Display(self._display_name)
        self._control_display = display.Display(self._display_name)
        if not self._record_display.has_extension("RECORD"):
            self._close_displays()
            raise RuntimeError("this X server has no RECORD extension")

        self._context = self._record_display.record_create_context(
            0,
            [record.AllClients],
            [{
                "core_requests": (0, 0),
                "core_replies": (0, 0),
                "ext_requests": (0, 0, 0, 0),
                "ext_replies": (0, 0, 0, 0),
                "delivered_events": (0, 0),
                "device_events": (X.KeyPress, X.MotionNotify),
                "errors": (0, 0),
                "client_started": False,
                "client_died": False,
            }],
        )

        self._recording = True
        self._origin = time.monotonic()
        self._started.clear()
        self._live = False
        self._thread = threading.Thread(
            target=self._capture, args=(sink,), daemon=True)
        self._thread.start()
        # The server sends StartOfData once the context is actually live. Waiting for
        # it is a handshake; sleeping a guessed interval instead loses whatever the
        # caller injects in the gap, which reads as flakiness rather than a bug.
        if not self._started.wait(5.0) or not self._live:
            self.stop()
            raise RuntimeError("XRecord context did not start")

    def _capture(self, sink: EventSink) -> None:
        def callback(reply):
            if reply.category == record.StartOfData:
                self._live = True
                self._started.set()
                return
            if reply.category != record.FromServer or reply.client_swapped:
                return
            if not len(reply.data) or reply.data[0] < 2:
                return
            data = reply.data
            while len(data):
                raw, data = rq.EventField(None).parse_binary_value(
                    data, self._record_display.display, None, None)
                event = self._translate(raw)
                if event is not None:
                    sink(time.monotonic() - self._origin, event)

        try:
            self._record_display.record_enable_context(self._context, callback)
            self._record_display.record_free_context(self._context)
        finally:
            self._started.set()  # unblock start() even if the enable failed

    def _translate(self, raw) -> Event | None:
        if raw.type == X.KeyPress:
            return KeyDown(self._sym_for(raw.detail))
        if raw.type == X.KeyRelease:
            return KeyUp(self._sym_for(raw.detail))
        if raw.type == X.MotionNotify:
            return Move(raw.root_x, raw.root_y)
        if raw.type == X.ButtonPress:
            if raw.detail in _SCROLL_BUTTONS:
                return Scroll(_SCROLL_BUTTONS[raw.detail], 1)
            return MouseDown(_button_name(raw.detail))
        if raw.type == X.ButtonRelease:
            if raw.detail in _SCROLL_BUTTONS:
                return None  # the press already stood for the whole detent
            return MouseUp(_button_name(raw.detail))
        return None

    def _sym_for(self, keycode: int) -> str:
        """Always the level-0 keysym. Any Shift the user held is captured as its own
        event, so replaying base keysyms plus those modifier events reproduces what
        was typed, and the file stays readable as `key a` rather than `key A`."""
        return keysym_name(self._record_display.keycode_to_keysym(keycode, 0))

    def stop(self) -> None:
        if not self._recording:
            return
        self._recording = False
        try:
            self._control_display.record_disable_context(self._context)
            self._control_display.flush()
        except Exception:  # pragma: no cover - the server may already be gone
            pass
        if self._thread is not None:
            self._thread.join(2.0)
            self._thread = None
        self._close_displays()

    def _close_displays(self) -> None:
        for attribute in ("_record_display", "_control_display"):
            dpy = getattr(self, attribute)
            if dpy is not None:
                try:
                    dpy.close()
                except Exception:  # pragma: no cover
                    pass
                setattr(self, attribute, None)

    @property
    def is_recording(self) -> bool:
        return self._recording


def _button_name(number: int) -> str:
    for name, value in _BUTTON_NUMBERS.items():
        if value == number:
            return name
    return "left"


#: A passive grab must name the exact modifier state, so a key has to be grabbed
#: once per lock combination to fire whatever CapsLock and NumLock are doing.
#:
#: `AnyModifier` looks like the shortcut for this and is a trap: the server refuses
#: it wholesale with BadAccess if *any* combination involving that key is already
#: grabbed by someone else. MATE binds Alt+Escape to window switching, so an
#: AnyModifier grab on Escape fails on an ordinary MATE desktop. Measured 2026-08-21.
LOCK_MASKS = (0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask)

#: Modifier names accepted in a hotkey like `Ctrl+Shift+A`.
MODIFIER_MASKS = {
    "ctrl": X.ControlMask,
    "control": X.ControlMask,
    "shift": X.ShiftMask,
    "alt": X.Mod1Mask,
    "meta": X.Mod1Mask,
    "super": X.Mod4Mask,
    "win": X.Mod4Mask,
}

#: Canonical order, so a parsed hotkey always formats back the same way.
MODIFIER_ORDER = (("ctrl", X.ControlMask), ("shift", X.ShiftMask),
                  ("alt", X.Mod1Mask), ("super", X.Mod4Mask))

#: The bits a hotkey can specify. Lock bits are deliberately excluded: CapsLock
#: and NumLock must never change whether a hotkey matches.
MODIFIER_BITS = X.ControlMask | X.ShiftMask | X.Mod1Mask | X.Mod4Mask

#: The physical keys behind each modifier bit, either side of the keyboard.
MODIFIER_SYMS_BY_BIT = {
    X.ControlMask: ("Control_L", "Control_R"),
    X.ShiftMask: ("Shift_L", "Shift_R"),
    X.Mod1Mask: ("Alt_L", "Alt_R"),
    X.Mod4Mask: ("Super_L", "Super_R"),
}


class GrabUnavailable(RuntimeError):
    """Another client already holds this key, so we cannot."""


def parse_hotkey(spec: str) -> tuple[int, str]:
    """`"Ctrl+Shift+A"` to `(mask, "A")`. Raises KeyResolutionError on nonsense.

    The key itself is normalised through the usual aliases, so `Ctrl+esc` and
    `Control+Escape` are the same hotkey.
    """
    text = (spec or "").strip()
    if not text:
        raise KeyResolutionError("empty hotkey")

    parts = text.split("+")
    # `Ctrl++` means Ctrl plus the plus key, and `+` alone is that key. A single
    # trailing plus (`Ctrl+`) is an unfinished hotkey, not a request for it.
    if len(parts) >= 2 and parts[-1] == "" and parts[-2] == "":
        parts = parts[:-2] + ["plus"]

    key = parts[-1].strip()
    if not key:
        raise KeyResolutionError(f"{spec!r} names modifiers but no key")

    mask = 0
    for token in parts[:-1]:
        name = token.strip().lower()
        if name not in MODIFIER_MASKS:
            raise KeyResolutionError(
                f"unknown modifier {token.strip()!r} in {spec!r}; use Ctrl, "
                f"Shift, Alt or Super")
        mask |= MODIFIER_MASKS[name]

    sym = normalise_key(key)
    # A letter is spelled upper case by convention (`Ctrl+A`); it names the key
    # rather than asking for Shift. Resolve to the lower-case keysym so the grab
    # does not quietly become Ctrl+Shift+A and leave Ctrl+A dead. Shift on a letter
    # has to be written out.
    if len(sym) == 1 and sym.isalpha():
        sym = sym.lower()
    return mask, sym


def format_hotkey(mask: int, sym: str) -> str:
    names = [label.capitalize() for label, bit in MODIFIER_ORDER if mask & bit]
    shown = sym.upper() if len(sym) == 1 and sym.isalpha() else sym
    return "+".join(names + [shown])


def hotkey_syms(spec: str) -> set:
    """Every keysym a hotkey involves, its modifier keys included.

    Recording captures the chord that stopped the take like any other keystroke,
    so trimming it back out needs the modifier keys as well as the final one.
    """
    try:
        mask, sym = parse_hotkey(spec)
    except KeyResolutionError:
        return set()
    out = {sym}
    for bit, names in MODIFIER_SYMS_BY_BIT.items():
        if mask & bit:
            out.update(names)
    return out


def normalise_hotkey(spec: str) -> str:
    """Round-trip a hotkey through the parser, giving it a canonical spelling."""
    return format_hotkey(*parse_hotkey(spec))


class HotkeyGrab:
    """Server-side passive grabs on one or more keys.

    Hotkeys cannot be widget keybindings: while a macro plays, and while the user
    is recording in some other application, this tool's window has no focus and
    would never see the key.
    """

    def __init__(self, display_name: str | None = None):
        self._display_name = display_name
        self._display = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        #: (keycode, modifier mask) -> action
        self._bound: dict[tuple[int, int], object] = {}
        self._specs: list[str] = []

    def start(self, bindings) -> None:
        """`bindings` maps a hotkey spec to a callable, where a spec is a keysym
        name with optional modifiers: `Escape`, `F9`, `Ctrl+Shift+A`. Empty specs
        are skipped, so an unbound hotkey costs nothing."""
        if self._thread is not None:
            raise RuntimeError("hotkey grab already active")
        wanted = {spec: action for spec, action in bindings.items() if spec}
        if not wanted:
            return

        self._display = display.Display(self._display_name)
        root = self._display.screen().root
        unavailable = []
        seen: dict[tuple[int, int], str] = {}
        for spec, action in wanted.items():
            mask, sym = parse_hotkey(spec)
            keycode, level = resolve_key(self._display, sym)
            # Punctuation that needs Shift to type at all, like `+`, gets it
            # supplied. Letters never do: see parse_hotkey.
            if level == 1:
                mask |= X.ShiftMask

            chord = (keycode, mask)
            if chord in seen:
                # Catch this here rather than letting the second grab collide with
                # our own first one, which would report BadAccess and blame some
                # other program. Two spellings of one chord look different as text.
                self.stop()
                raise GrabUnavailable(
                    f"{spec!r} and {seen[chord]!r} are the same combination")
            seen[chord] = spec

            if self._grab_all_locks(root, keycode, mask):
                self._bound[(keycode, mask)] = action
                self._specs.append(format_hotkey(mask, sym))
            else:
                unavailable.append(spec)

        if unavailable:
            self.stop()
            raise GrabUnavailable(
                "another program already holds " + ", ".join(sorted(unavailable)))

        self._stop.clear()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()

    def _grab_all_locks(self, root, keycode: int, mask: int) -> bool:
        """True if the key was grabbed. X reports grab failures asynchronously, so
        `grab_key` never raises: without an explicit error handler and a sync, a
        refused grab looks exactly like a successful one and the hotkey is simply
        dead."""
        taken = 0
        for lock in LOCK_MASKS:
            caught = error.CatchError()
            root.grab_key(keycode, mask | lock, True, X.GrabModeAsync,
                          X.GrabModeAsync, onerror=caught)
            self._display.sync()
            if caught.get_error() is None:
                taken += 1
        return taken > 0

    def _watch(self) -> None:
        fileno = self._display.fileno()
        while not self._stop.is_set():
            # select rather than next_event(), which would block past stop().
            readable, _, _ = select.select([fileno], [], [], 0.1)
            if not readable:
                continue
            for _ in range(self._display.pending_events()):
                event = self._display.next_event()
                if event.type != X.KeyPress:
                    continue
                # Mask off CapsLock and NumLock: they must never decide whether a
                # hotkey matches. Two hotkeys can share a keycode (Ctrl+A and
                # Shift+A), so the modifier state is part of the lookup.
                state = event.state & MODIFIER_BITS
                action = self._bound.get((event.detail, state))
                if action is not None:
                    action()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2.0)
            self._thread = None
        if self._display is not None:
            try:
                root = self._display.screen().root
                for keycode, mask in self._bound:
                    for lock in LOCK_MASKS:
                        root.ungrab_key(keycode, mask | lock)
                self._display.sync()
                self._display.close()
            except Exception:  # pragma: no cover
                pass
            self._display = None
        self._bound = {}
        self._specs = []

    @property
    def is_active(self) -> bool:
        return self._thread is not None

    @property
    def grabbed(self) -> list[str]:
        """Canonical spellings of what is currently held."""
        return sorted(self._specs)


def panic_skip_sym(panic_hotkey: str) -> str | None:
    """Which keysym playback must not inject, or None.

    Only an unmodified panic key is skipped. If the panic hotkey is `Ctrl+Escape`,
    a macro's plain Escape cannot trigger it, and refusing to type Escape at all
    would break perfectly good macros for no reason.
    """
    try:
        mask, sym = parse_hotkey(panic_hotkey)
    except KeyResolutionError:
        return None
    return sym if mask == 0 else None


def macro_warnings(macro, dpy=None, panic_sym: str = "Escape") -> list[str]:
    """Everything worth telling the user at load time, before anything is injected.

    Warn-and-skip rather than refuse: a macro containing the panic key still plays,
    minus those keystrokes.
    """
    warnings = []
    owns = dpy is None
    if owns:
        dpy = display.Display()
    try:
        layout = current_layout(dpy)
        if macro.layout and layout and macro.layout != layout:
            warnings.append(
                f"macro was recorded on layout {macro.layout!r} but this keyboard "
                f"is {layout!r}; keys may not match")

        skip = panic_skip_sym(panic_sym)
        panic_seen = False
        unresolved = []
        for sym in _macro_key_syms(macro):
            if skip is not None and sym == skip:
                panic_seen = True
                continue
            try:
                _, level = resolve_key(dpy, sym)
                modifier_keycodes(dpy, level)
            except KeyResolutionError:
                if sym not in unresolved:
                    unresolved.append(sym)
        if panic_seen:
            warnings.append(
                f"macro contains the panic key {skip!r}; those keystrokes "
                f"will be skipped during playback")
        if unresolved:
            warnings.append(
                "this keyboard cannot produce: " + ", ".join(unresolved))
    finally:
        if owns:
            dpy.close()
    return warnings


def _macro_key_syms(macro):
    for event in macro.events:
        if isinstance(event, (KeyTap, KeyDown, KeyUp)):
            yield event.sym
        elif isinstance(event, TypeText):
            for tap in expand_type(event.text):
                yield tap.sym
