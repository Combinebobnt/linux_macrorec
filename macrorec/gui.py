"""The PyQt5 window: a compact transport bar.

Deliberately not an editor. Macro files are plain text, so editing belongs in
whatever editor the user already has; this window records, plays, loops and reloads.

The backend factories are injectable so the state machine can be tested without an X
server. Nothing here imports Xlib at module scope.
"""

from __future__ import annotations

import os

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .collapse import accumulate_motion, collapse_motion, merge_sleeps, sample_motion
from .events import MOUSE_EVENTS, KeyDown, KeyUp, Macro, Move, Sleep
from .playback import Playback
from .script import ScriptError, format_macro, parse
from .settings import Settings
from .timeline import build_schedule, to_events

FILE_FILTER = "Macro files (*.macro *.txt);;All files (*)"

IDLE = "idle"
RECORDING = "recording"
PLAYING = "playing"


def _default_recorder(capture_raw_input):
    if capture_raw_input:
        from .backend.xi2 import XI2Recorder
        return XI2Recorder()
    from .backend.x11 import X11Recorder
    return X11Recorder()


def _default_player(skip_syms):
    from .backend.x11 import X11Player
    return X11Player(skip_syms=skip_syms)


def _default_grab():
    from .backend.x11 import HotkeyGrab
    return HotkeyGrab()


def _default_game_grab():
    from .backend.xi2 import RawHotkeyWatch
    return RawHotkeyWatch()


def _default_warnings(macro, panic_sym, panic_key_is_withheld):
    from .backend.x11 import macro_warnings
    return macro_warnings(
        macro, panic_sym=panic_sym, panic_key_is_withheld=panic_key_is_withheld)


def _default_key_check(spec):
    """True if this keyboard can produce the hotkey `spec`, which may carry
    modifiers (`Ctrl+Shift+A`). A panic key that cannot be resolved leaves playback
    with no working stop, so the settings dialog rejects it."""
    from Xlib import display

    from .backend.x11 import KeyResolutionError, parse_hotkey, resolve_key

    connection = display.Display()
    try:
        _, sym = parse_hotkey(spec)
        resolve_key(connection, sym)
        return True
    except KeyResolutionError:
        return False
    finally:
        connection.close()


def _default_panic_skip(panic_hotkey):
    from .backend.x11 import panic_skip_sym
    return panic_skip_sym(panic_hotkey)


def _default_hotkey_syms(spec):
    from .backend.x11 import hotkey_syms
    return hotkey_syms(spec)


def _normalise_hotkey(spec):
    """Canonical spelling for a hotkey. Pure parsing, no display needed, but it
    lives in the X backend because the modifier names are X's."""
    from .backend.x11 import normalise_hotkey
    return normalise_hotkey(spec)


class _Bridge(QObject):
    """Carries worker-thread callbacks onto the GUI thread. Touching widgets from
    the playback or panic-grab thread is undefined behaviour in Qt."""

    stepped = pyqtSignal(int, int)
    finished = pyqtSignal(bool, object)
    panicked = pyqtSignal()
    hotkey = pyqtSignal(str)


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None,
                 key_check=_default_key_check):
        super().__init__(parent)
        self.setWindowTitle("macrorec settings")
        self.settings = settings
        self._key_check = key_check

        hint = ("A key name, with optional modifiers:\n"
                "Escape, F12, Ctrl+Shift+A, Alt+Pause, Super+r.\n"
                "Modifiers are Ctrl, Shift, Alt and Super.")

        self.panic_edit = QLineEdit(settings.panic_key)
        self.panic_edit.setToolTip(
            hint + "\n\nHeld as a global grab while a macro plays, so it works "
            "even though this window has no focus.")

        self.record_edit = QLineEdit(settings.record_key)
        self.record_edit.setPlaceholderText("unbound")
        self.record_edit.setToolTip(
            "Optional. Starts recording, and stops it again.\n"
            "Leave empty to bind nothing.\n\n" + hint)

        self.play_edit = QLineEdit(settings.play_key)
        self.play_edit.setPlaceholderText("unbound")
        self.play_edit.setToolTip(
            "Optional. Starts playback.\n"
            "Leave empty to bind nothing.\n\n" + hint)

        globals_form = QFormLayout()
        globals_form.addRow("Panic:", self.panic_edit)
        globals_form.addRow("Record:", self.record_edit)
        globals_form.addRow("Play:", self.play_edit)

        globals_box = QGroupBox("Global hotkeys (work in any window)")
        globals_box.setLayout(globals_form)

        self.window_edits = {}
        window_form = QFormLayout()
        for label, field in (("Open:", "open_key"), ("Save:", "save_key"),
                             ("Save As:", "save_as_key"),
                             ("Reload:", "reload_key")):
            edit = QLineEdit(getattr(settings, field))
            edit.setPlaceholderText("unbound")
            edit.setToolTip(
                "Only active while this window has focus.\n"
                "Leave empty to bind nothing.")
            self.window_edits[field] = edit
            window_form.addRow(label, edit)

        # Keep both titles short enough to fit: a QGroupBox title wider than its
        # box is clipped rather than wrapped or grown.
        window_box = QGroupBox("Window shortcuts (only when focused)")
        window_box.setLayout(window_form)

        self.motion_path_check = QCheckBox("Capture mouse movement paths")
        self.motion_path_check.setChecked(settings.capture_motion_path)
        self.motion_path_check.setToolTip(
            "Record the route the pointer takes, not just the position where each\n"
            "click happens. Drags and freehand strokes then replay along the same\n"
            "path at the same speed.\n\n"
            "Off by default: it makes macro files far longer and harder to edit by\n"
            "hand. Takes effect on the next recording.")

        self.raw_input_check = QCheckBox("Capture raw input (for fullscreen games)")
        self.raw_input_check.setChecked(settings.capture_raw_input)
        self.raw_input_check.setToolTip(
            "Record via XI2 raw input instead of the ordinary capture. Needed for\n"
            "mouselook in a fullscreen game: it grabs the pointer and warps it back\n"
            "to centre every frame, which hides the real motion from ordinary\n"
            "recording entirely.\n\n"
            "Also moves the panic stop to a passive XI2 watcher, since a game's own\n"
            "exclusive keyboard grab blocks the ordinary one too - the panic key\n"
            "will then also reach the game, opening its menu, rather than being\n"
            "withheld from it. The watcher tells its own injected keys from real\n"
            "ones, so a macro may now contain the panic key itself and it plays\n"
            "back and types normally instead of being skipped with a warning.\n\n"
            "Off by default. Takes effect on the next recording and the next play.")

        recording_layout = QVBoxLayout()
        recording_layout.addWidget(self.motion_path_check)
        recording_layout.addWidget(self.raw_input_check)

        recording_box = QGroupBox("Recording")
        recording_box.setLayout(recording_layout)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(globals_box)
        layout.addWidget(window_box)
        layout.addWidget(recording_box)
        layout.addWidget(buttons)

    def accept(self) -> None:
        entered = {
            "Panic": self.panic_edit.text().strip(),
            "Record": self.record_edit.text().strip(),
            "Play": self.play_edit.text().strip(),
        }
        if not entered["Panic"]:
            QMessageBox.warning(self, "macrorec", "The panic key cannot be empty.")
            return

        cleaned = {}
        # Record and Play may be empty: unbound is their default.
        for label, spec in entered.items():
            if not spec:
                cleaned[label] = ""
                continue
            try:
                canonical = _normalise_hotkey(spec)
            except Exception as exc:
                QMessageBox.warning(
                    self, "macrorec",
                    f"The {label} hotkey {spec!r} could not be understood:\n"
                    f"{exc}\n\nWrite a key name with optional modifiers, such as "
                    f"Escape, F12, or Ctrl+Shift+A.")
                return
            try:
                usable = self._key_check(canonical)
            except Exception:
                usable = True  # no display to check against; do not block the user
            if not usable:
                QMessageBox.warning(
                    self, "macrorec",
                    f"{spec!r} is not a key this keyboard can produce, so the "
                    f"{label} hotkey would never fire.\n\nUse a key name such as "
                    f"Escape or F12, optionally with Ctrl, Shift, Alt or Super.")
                return
            cleaned[label] = canonical

        window_keys = {}
        for field, edit in self.window_edits.items():
            spec = edit.text().strip()
            if not spec:
                window_keys[field] = ""
                continue
            canonical = QKeySequence(spec).toString()
            if not canonical:
                QMessageBox.warning(
                    self, "macrorec",
                    f"{spec!r} is not a shortcut Qt understands.\n\nWrite it like "
                    f"Ctrl+O or Ctrl+Shift+S.")
                return
            window_keys[field] = canonical

        # Compare canonical forms, so `ctrl+a` and `Ctrl+A` count as a clash. A
        # global hotkey beats a window shortcut whenever both match, because the
        # server-side grab intercepts the key before Qt ever sees it, so the two
        # groups are checked together rather than separately.
        bound = [spec for spec in cleaned.values() if spec]
        bound += [spec for spec in window_keys.values() if spec]
        folded = [spec.lower() for spec in bound]
        if len(set(folded)) != len(folded):
            QMessageBox.warning(
                self, "macrorec",
                "Two keybinds are set to the same combination. Give each a "
                "different one.\n\nA global hotkey always wins over a window "
                "shortcut, so the window one would simply never fire.")
            return

        self.settings.panic_key = cleaned["Panic"]
        self.settings.record_key = cleaned["Record"]
        self.settings.play_key = cleaned["Play"]
        for field, spec in window_keys.items():
            setattr(self.settings, field, spec)
        # Nothing to validate on a checkbox, so it is assigned with the rest, after
        # every early return that leaves settings untouched.
        self.settings.capture_motion_path = self.motion_path_check.isChecked()
        self.settings.capture_raw_input = self.raw_input_check.isChecked()
        super().accept()


class MacroRecWindow(QMainWindow):
    def __init__(self, settings: Settings | None = None, *,
                 recorder_factory=_default_recorder,
                 player_factory=_default_player,
                 grab_factory=_default_grab,
                 game_grab_factory=_default_game_grab,
                 warnings_factory=_default_warnings,
                 panic_skip=_default_panic_skip,
                 hotkey_syms=_default_hotkey_syms,
                 settings_path: str | None = None,
                 parent=None):
        super().__init__(parent)
        self.settings = settings if settings is not None else Settings.load()
        self.settings_path = settings_path
        self._recorder_factory = recorder_factory
        self._player_factory = player_factory
        self._grab_factory = grab_factory
        self._game_grab_factory = game_grab_factory
        self._warnings_factory = warnings_factory
        self._panic_skip = panic_skip
        self._hotkey_syms_for = hotkey_syms

        self.macro = Macro()
        self.path: str | None = None
        self.mode = IDLE

        self._recorder = None
        self._captured: list = []
        self._stopped_by_click = False
        self._playback: Playback | None = None
        self._player = None
        self._grab = None
        self._hotkey_syms: dict[str, str] = {}
        self._stopped_by_hotkey: str | None = None

        self._tick = QTimer(self)
        self._tick.setInterval(200)
        self._tick.timeout.connect(self._refresh)

        self.bridge = _Bridge()
        self.bridge.stepped.connect(self._on_stepped)
        self.bridge.finished.connect(self._on_finished)
        self.bridge.panicked.connect(self._on_panicked)
        self.bridge.hotkey.connect(self._on_hotkey)

        self.setWindowTitle("macrorec")
        self._build_ui()
        self._apply_settings()
        self._refresh()
        self._rebind_hotkeys()

    # --- construction --------------------------------------------------------

    def _build_ui(self) -> None:
        self.record_button = QPushButton("● Rec")
        self.record_button.setToolTip("Record a new macro, replacing the current one")
        self.record_button.clicked.connect(self.start_recording)

        self.play_button = QPushButton("▶ Play")
        self.play_button.clicked.connect(self.start_playback)

        self.stop_button = QPushButton("■ Stop")
        self.stop_button.clicked.connect(self._stop_clicked)

        transport = QHBoxLayout()
        for button in (self.record_button, self.play_button, self.stop_button):
            transport.addWidget(button)

        self.loop_spin = QSpinBox()
        self.loop_spin.setRange(0, 9999)
        self.loop_spin.setSpecialValueText("forever")  # 0
        self.loop_spin.setValue(1)

        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(0.05, 20.0)
        self.speed_spin.setSingleStep(0.25)
        self.speed_spin.setDecimals(2)
        self.speed_spin.setSuffix("x")
        self.speed_spin.setValue(1.0)
        self.speed_spin.setToolTip(
            "Divides every delay, explicit sleep lines included.\n"
            "It scales the whole macro's tempo.")

        options = QHBoxLayout()
        options.addWidget(QLabel("Loop"))
        options.addWidget(self.loop_spin)
        options.addSpacing(12)
        options.addWidget(QLabel("Speed"))
        options.addWidget(self.speed_spin)
        options.addStretch(1)

        rule = QFrame()
        rule.setFrameShape(QFrame.HLine)
        rule.setFrameShadow(QFrame.Sunken)

        self.file_label = QLabel("(unsaved macro)")
        self.file_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.status_label = QLabel("0 steps")
        self.on_top_check = QCheckBox("on top")
        self.on_top_check.toggled.connect(self._set_always_on_top)

        status = QHBoxLayout()
        status.addWidget(self.status_label)
        status.addStretch(1)
        status.addWidget(self.on_top_check)

        layout = QVBoxLayout()
        layout.addLayout(transport)
        layout.addLayout(options)
        layout.addWidget(rule)
        layout.addWidget(self.file_label)
        layout.addLayout(status)

        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)

        self._build_toolbar()

    def _build_toolbar(self) -> None:
        toolbar = self.addToolBar("Main")
        toolbar.setMovable(False)

        self.open_action = QAction("Open", self)
        self.open_action.triggered.connect(self.open_file)

        self.save_action = QAction("Save", self)
        self.save_action.triggered.connect(self.save_file)

        self.save_as_action = QAction("Save As", self)
        self.save_as_action.triggered.connect(self.save_file_as)

        self.reload_action = QAction("Reload", self)
        self.reload_action.setToolTip("Re-read the file after editing it elsewhere")
        self.reload_action.triggered.connect(self.reload_file)

        #: Which setting drives which action's shortcut.
        self.shortcut_actions = {
            "open_key": self.open_action,
            "save_key": self.save_action,
            "save_as_key": self.save_as_action,
            "reload_key": self.reload_action,
        }
        self._apply_shortcuts()

        self.settings_action = QAction("Settings", self)
        self.settings_action.triggered.connect(self.open_settings)

        for action in (self.open_action, self.save_action, self.save_as_action,
                       self.reload_action, self.settings_action):
            toolbar.addAction(action)

    def _apply_shortcuts(self) -> None:
        """Window shortcuts come from settings, not from literals at the call site,
        so the settings dialog can list them and changing one takes effect at once."""
        for field, action in self.shortcut_actions.items():
            spec = getattr(self.settings, field, "")
            action.setShortcut(QKeySequence(spec) if spec else QKeySequence())

    def _apply_settings(self) -> None:
        self.loop_spin.setValue(self.settings.loops)
        self.speed_spin.setValue(self.settings.speed)
        self.on_top_check.setChecked(self.settings.always_on_top)

    # --- state ---------------------------------------------------------------

    def _refresh(self) -> None:
        idle = self.mode == IDLE
        has_events = bool(self.macro.events)
        # Record and Play are mutually exclusive: an active record context would
        # otherwise capture the player's own injected events.
        self.record_button.setEnabled(idle)
        self.play_button.setEnabled(idle and has_events)
        self.stop_button.setEnabled(not idle)
        for action in (self.open_action, self.settings_action):
            action.setEnabled(idle)
        self.reload_action.setEnabled(idle and self.path is not None)
        self.save_action.setEnabled(idle and has_events)
        self.save_as_action.setEnabled(idle and has_events)
        self.loop_spin.setEnabled(idle)
        self.speed_spin.setEnabled(idle)

        self.file_label.setText(
            os.path.basename(self.path) if self.path else "(unsaved macro)")
        if self.mode == RECORDING:
            self.status_label.setText(f"recording... {len(self._captured)} events")
        elif self.mode != PLAYING:
            self.status_label.setText(self._describe())

    def _describe(self) -> str:
        count = len(self.macro.events)
        if not count:
            return "0 steps"
        duration = build_schedule(self.macro.events, self._speed()).duration
        return f"{count} steps · {duration:.1f}s"

    def _speed(self) -> float:
        return float(self.speed_spin.value())

    # --- global hotkeys ------------------------------------------------------

    def _bindings_for_mode(self) -> dict[str, str]:
        """Which keys to grab right now, as sym -> action.

        Grabs are held per mode rather than all at once. A key the server has
        given us is taken away from every other program, so macrorec holds only
        what it can currently act on: the panic key exists only during playback,
        and Record doubles as Stop while recording.
        """
        if self.mode == PLAYING:
            return {self.settings.panic_key: "panic"}
        if self.mode == RECORDING:
            return {self.settings.record_key: "stop"} if self.settings.record_key else {}
        bindings = {}
        if self.settings.record_key:
            bindings[self.settings.record_key] = "record"
        if self.settings.play_key:
            bindings[self.settings.play_key] = "play"
        return bindings

    def _use_game_grab(self) -> bool:
        """During playback, a game may hold an exclusive keyboard grab that
        `HotkeyGrab`'s `XGrabKey` would never see past - the same reason
        `capture_raw_input` swaps the recorder for `XI2Recorder`. Gated on the
        setting rather than used unconditionally: `RawHotkeyWatch` is a passive
        watch, not a grab, so the panic key would also reach whatever window is
        focused, which is only an acceptable trade while chasing a game."""
        return self.mode == PLAYING and self.settings.capture_raw_input

    def _rebind_hotkeys(self) -> None:
        if self._grab is not None:
            self._grab.stop()
            self._grab = None
        bindings = {sym: action
                    for sym, action in self._bindings_for_mode().items() if sym}
        self._hotkey_syms = bindings
        if not bindings:
            return
        factory = self._game_grab_factory if self._use_game_grab() else self._grab_factory
        try:
            grab = factory()
            grab.start({
                sym: (lambda action=action: self.bridge.hotkey.emit(action))
                for sym, action in bindings.items()
            })
            self._grab = grab
        except Exception as exc:
            self._grab = None
            self._report_grab_failure(exc, bindings)

    def _report_grab_failure(self, exc, bindings) -> None:
        keys = ", ".join(sorted(bindings))
        if self.mode == PLAYING:
            QMessageBox.warning(
                self, "macrorec",
                f"The {keys} panic stop could not be armed:\n{exc}\n\n"
                f"The macro will still play, but stopping it means giving this "
                f"window focus and pressing Stop. Another key may work better; "
                f"try changing it under Settings.")
        else:
            QMessageBox.warning(
                self, "macrorec",
                f"The hotkey {keys} could not be registered:\n{exc}\n\n"
                f"Use the buttons, or pick a different key under Settings.")

    def _on_hotkey(self, action: str) -> None:
        """Dispatched on the GUI thread; the grab's watch thread only emits."""
        if action == "panic":
            self._on_panicked()
        elif action == "record" and self.mode == IDLE:
            self.start_recording()
        elif action == "play" and self.mode == IDLE:
            self.start_playback()
        elif action == "stop" and self.mode == RECORDING:
            self._stopped_by_hotkey = self.settings.record_key
            self.stop()

    def _set_always_on_top(self, enabled: bool) -> None:
        # Ask before changing the flag, never after. setWindowFlag() re-parents the
        # widget, and Qt does that by hiding it, so isVisible() afterwards is always
        # False and a guard reading it there would leave the window unmapped.
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, enabled)
        self.settings.always_on_top = enabled
        if was_visible:
            self.show()  # the flag only takes effect on re-show

    # --- recording -----------------------------------------------------------

    def start_recording(self) -> None:
        if self.mode != IDLE:
            return
        self._captured = []
        try:
            self._recorder = self._recorder_factory(self.settings.capture_raw_input)
            self._recorder.start(self._on_captured)
        except Exception as exc:
            self._recorder = None
            self._error("Could not start recording", exc)
            return
        # Replaces the current macro outright, no prompt.
        self.macro = Macro()
        self.path = None
        self.mode = RECORDING
        self._tick.start()  # the captured count only moves if something repaints it
        self._rebind_hotkeys()
        self._refresh()

    def _on_captured(self, at, event) -> None:
        """Called on the recorder thread. Appending to a list is all that happens
        here; building the macro waits until stop, on the GUI thread."""
        self._captured.append((at, event))

    def _finish_recording(self) -> None:
        self._tick.stop()
        recorder, self._recorder = self._recorder, None
        if recorder is not None:
            recorder.stop()
        captured = list(self._captured)
        # Read the preference here, not at construction, so toggling it applies to
        # the next recording rather than the next launch. Sampling/accumulating run
        # before to_events; collapsing runs after. See collapse.sample_motion for
        # why. capture_raw_input takes priority: XI2Recorder emits MoveRel, which
        # collapse_motion cannot touch (collapsing deltas to their last one is
        # meaningless) and sample_motion cannot either (it keeps the last sample
        # rather than summing, which would throw away most of a fast turn).
        if self.settings.capture_raw_input:
            events = to_events(accumulate_motion(captured))
        elif self.settings.capture_motion_path:
            events = to_events(sample_motion(captured))
        else:
            events = collapse_motion(to_events(captured))
        by_click, self._stopped_by_click = self._stopped_by_click, False
        by_hotkey, self._stopped_by_hotkey = self._stopped_by_hotkey, None
        if by_click:
            # In game mode there is no absolute Move at all, only MoveRel, so
            # _trim_own_interaction's window-geometry scan always no-ops here: a
            # stated restriction (AGENTS.md), not a silent gap. Recording is
            # stopped with a hotkey in a fullscreen game anyway, where this path
            # is unreachable.
            events = self._trim_own_interaction(events)
        elif by_hotkey:
            events = self._trim_trailing_key(events, by_hotkey)
        # Last, so it normalises whatever the trims left behind. Order against them is
        # immaterial either way: both pop a whole run of trailing sleeps, not one.
        events = merge_sleeps(events)
        self.macro = Macro(events=events)
        self._set_layout_header()

    def _trim_trailing_key(self, events: list, spec: str) -> list:
        """Drop the hotkey press that ended the recording. Grabbing a key routes it
        to us, but XRecord still sees it, so it lands in the macro like any other
        keystroke.

        The whole chord goes, modifier keys included: trimming only the final key
        would leave `keydown ctrl` / `keydown shift` in every macro.
        """
        try:
            syms = self._hotkey_syms_for(spec)
        except Exception:
            syms = set()
        if not syms:
            return events

        trimmed = list(events)
        while trimmed and isinstance(trimmed[-1], (KeyDown, KeyUp, Sleep)):
            last = trimmed[-1]
            if isinstance(last, Sleep) or last.sym in syms:
                trimmed.pop()
                continue
            break
        while trimmed and isinstance(trimmed[-1], Sleep):
            trimmed.pop()
        return trimmed

    def _trim_own_interaction(self, events: list) -> list:
        """Drop the click on our own Stop button that ended the recording.

        XRecord taps every client's events, so pressing Stop is captured like any
        other click and every macro would otherwise end by clicking wherever this
        window happened to be.

        With motion-path capture on there is a whole run of moves walking the pointer
        here, not one, so the run goes too. The walk stops at the first move outside
        our rect, and at any event that is not a move or a sleep, which keeps it to
        the approach contiguous with the click that ended the recording: an earlier
        click inside our rect that the macro genuinely wanted is left alone.
        """
        rect = self.frameGeometry()
        last_move = None
        for index in range(len(events) - 1, -1, -1):
            if isinstance(events[index], Move):
                last_move = index
                break
        if last_move is None:
            return events
        move = events[last_move]
        if not rect.contains(move.x, move.y):
            return events
        if not any(isinstance(e, MOUSE_EVENTS) for e in events[last_move + 1:]):
            return events

        start = last_move
        for index in range(last_move - 1, -1, -1):
            event = events[index]
            if isinstance(event, Sleep):
                continue
            if isinstance(event, Move) and rect.contains(event.x, event.y):
                start = index
                continue
            break

        trimmed = events[:start]
        while trimmed and isinstance(trimmed[-1], Sleep):
            trimmed.pop()
        return trimmed

    def _set_layout_header(self) -> None:
        try:
            from .backend.x11 import current_layout
            from Xlib import display

            connection = display.Display()
            try:
                self.macro.layout = current_layout(connection)
            finally:
                connection.close()
        except Exception:
            self.macro.layout = None

    # --- playback ------------------------------------------------------------

    def start_playback(self) -> None:
        if self.mode != IDLE or not self.macro.events:
            return

        warnings = self._collect_warnings()
        if warnings:
            QMessageBox.warning(self, "macrorec", "\n\n".join(warnings))

        # Only an unmodified panic key is withheld from playback: a macro's plain
        # Escape cannot trigger a Ctrl+Escape panic stop. With capture_raw_input
        # on, nothing is withheld at all: RawHotkeyWatch filters its own injected
        # keys by sourceid instead, so the panic key can be typed without
        # stopping the macro that types it - see AGENTS.md.
        if self.settings.capture_raw_input:
            skip = None
        else:
            try:
                skip = self._panic_skip(self.settings.panic_key)
            except Exception:
                skip = None
        try:
            self._player = self._player_factory({skip} if skip else set())
        except Exception as exc:
            self._player = None
            self._error("Could not open the display for playback", exc)
            return

        schedule = build_schedule(self.macro.events, self._speed())
        self._playback = Playback(
            self._player, schedule, loops=self.loop_spin.value(),
            on_step=lambda loop, step, _: self.bridge.stepped.emit(loop, step),
            on_finish=lambda stopped, error: self.bridge.finished.emit(
                stopped, error))

        self.mode = PLAYING
        # Arm the panic grab before a single event is injected, never after.
        self._rebind_hotkeys()
        self._refresh()
        self.status_label.setText("playing...")
        self._playback.start()

    def _collect_warnings(self) -> list[str]:
        try:
            return list(self._warnings_factory(
                self.macro, self.settings.panic_key,
                not self.settings.capture_raw_input))
        except Exception:
            return []

    def _on_stepped(self, loop_index: int, step_index: int) -> None:
        total = len(self.macro.events)
        loops = self.loop_spin.value()
        where = f"step {step_index + 1}/{total}"
        if loops == 1:
            self.status_label.setText(f"playing... {where}")
        elif loops == 0:
            self.status_label.setText(f"looping... pass {loop_index + 1}, {where}")
        else:
            self.status_label.setText(
                f"looping... pass {loop_index + 1}/{loops}, {where}")

    def _on_finished(self, stopped: bool, error) -> None:
        self._teardown_playback()
        self.mode = IDLE
        self._rebind_hotkeys()  # drop the panic grab, take the idle hotkeys back
        self._refresh()
        if error is not None:
            self._error("Playback failed", error)
        elif stopped:
            self.status_label.setText("stopped")

    def _on_panicked(self) -> None:
        if self._playback is not None:
            self._playback.stop()

    def _teardown_playback(self) -> None:
        if self._player is not None:
            self._player.close()
            self._player = None
        self._playback = None

    # --- stop ----------------------------------------------------------------

    def _stop_clicked(self) -> None:
        """Stop, knowing the mouse did it. Only then is a trailing click on this
        window ours to remove rather than part of the macro."""
        self._stopped_by_click = True
        self.stop()

    def stop(self) -> None:
        if self.mode == RECORDING:
            self._finish_recording()
            self.mode = IDLE
            self._rebind_hotkeys()
            self._refresh()
        elif self.mode == PLAYING and self._playback is not None:
            self._playback.stop()

    # --- files ---------------------------------------------------------------

    def open_file(self, path: str | None = None) -> None:
        if self.mode != IDLE:
            return
        if not path:
            path, _ = QFileDialog.getOpenFileName(
                self, "Open macro", self.settings.last_directory, FILE_FILTER)
        if not path:
            return
        if self._load(path):
            self.path = path
            self.settings.last_directory = os.path.dirname(path)
            self._refresh()

    def reload_file(self) -> None:
        if self.mode == IDLE and self.path and self._load(self.path):
            self._refresh()

    def _load(self, path: str) -> bool:
        try:
            with open(path, encoding="utf-8") as handle:
                macro = parse(handle.read())
        except ScriptError as exc:
            self._error(f"{os.path.basename(path)} could not be parsed", exc)
            return False
        except OSError as exc:
            self._error("Could not read the file", exc)
            return False

        self.macro = macro
        # The file's own speed header seeds the control, which then governs.
        self.speed_spin.setValue(macro.speed)
        warnings = self._collect_warnings()
        if warnings:
            QMessageBox.warning(self, "macrorec", "\n\n".join(warnings))
        return True

    def save_file(self) -> None:
        if self.path:
            self._write(self.path)
        else:
            self.save_file_as()

    def save_file_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save macro", self.settings.last_directory, FILE_FILTER)
        if not path:
            return
        if self._write(path):
            self.path = path
            self.settings.last_directory = os.path.dirname(path)
            self._refresh()

    def _write(self, path: str) -> bool:
        self.macro.speed = self._speed()
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(format_macro(self.macro))
        except OSError as exc:
            self._error("Could not write the file", exc)
            return False
        return True

    # --- settings ------------------------------------------------------------

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec_() == QDialog.Accepted:
            self._rebind_hotkeys()  # the keys to hold may have just changed
            self._apply_shortcuts()

    def _persist_settings(self) -> None:
        self.settings.loops = self.loop_spin.value()
        self.settings.speed = self._speed()
        self.settings.always_on_top = self.on_top_check.isChecked()
        try:
            self.settings.save(self.settings_path)
        except OSError:
            pass

    # --- shutdown ------------------------------------------------------------

    def closeEvent(self, event):  # noqa: N802 - Qt naming
        if self.mode == PLAYING and self._playback is not None:
            self._playback.stop()
            self._playback.wait(2.0)
            self._teardown_playback()
        elif self.mode == RECORDING:
            self._finish_recording()
        if self._grab is not None:
            self._grab.stop()  # hand the grabbed keys back to the desktop
            self._grab = None
        self._persist_settings()
        super().closeEvent(event)

    def _error(self, title: str, exc) -> None:
        self.status_label.setText(title.lower())
        QMessageBox.critical(self, "macrorec", f"{title}:\n{exc}")


def main(argv=None) -> int:
    import sys

    argv = list(sys.argv if argv is None else argv)
    app = QApplication(argv)
    window = MacroRecWindow()
    if len(argv) > 1:
        window.open_file(argv[1])
    window.show()
    _signal_ready()
    return app.exec_()


def _signal_ready() -> None:
    """Tell bootstrap.py the window is up, so its splash closes when macrorec
    appears rather than while it is still starting."""
    path = os.environ.get("MACROREC_READY_FILE")
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("ready\n")
    except OSError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
