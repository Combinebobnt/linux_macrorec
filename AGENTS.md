# macrorec

A GUI tool to record keyboard and mouse input on X11, replay it, loop it, and save it as a
human-readable text file that can be hand-edited afterwards. X11 only, by design.

## Setup and verification

`./LAUNCH_macrorec_LinuxMac.sh` does the whole setup and starts the app: it finds a Python 3 and
hands off to `bootstrap.py`, which builds the venv, installs `requirements.txt`, and launches.
That is the path a user takes, and it is the one to check still works after touching packaging.

For development, the same thing by hand:

```
/usr/bin/python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest
```

Both details in the venv line matter:

- `/usr/bin/python3` by absolute path. Plain `python3` is a pyenv shim, and PyQt5 lives in Debian's
  `dist-packages`.
- `--system-site-packages`, so PyQt5 and PIL stay visible while python-xlib installs locally. With
  it, pip reports a distribution-packaged PyQt5 as already satisfied and downloads nothing for it,
  which is why `requirements.txt` can list PyQt5 unconditionally.

**Dependencies are declared twice**, in `pyproject.toml` and `requirements.txt`, because
`bootstrap.py` must not import the project to find them. `tests/test_bootstrap.py` fails if the two
disagree, so add a dependency to both.

`pytest` is the verification step for every change. Do not claim a change works without running it.
The X backend tests spawn a headless Xvfb and take a few seconds; the rest need no display and are
instant. Tests skip rather than fail when `Xvfb` or `python-xlib` is missing.

**There are two headless displays, and the difference matters.** The `xvfb` fixture is a bare
server with nothing managing windows, which is what most tests want. The `wm_display` fixture is a
second server running **real `marco`**, MATE's window manager, and it exists because the Escape
panic stop shipped broken: marco binds Alt+Escape, which makes an `AnyModifier` grab on Escape fail
wholesale. Grab behaviour is therefore tested against the actual window manager rather than a
stand-in for it. `wm_display` is session-scoped and lazy, so it costs nothing unless a test asks
for it, and it skips when `marco` is absent.

Assert on what XRecord observed, never on a client window receiving input. Xvfb has no window
manager and no mapped client, so XTEST events reach no focus window; XRecord taps the stream
server-side and sees them regardless. A test written the other way reports a false failure.

## Environment facts, do not "correct" these

Measured on the development machine, and the test fixtures depend on all three:

- **Xvfb has to listen on TCP.** Its unix socket fails here with `_XSERVTransSocketOpenCOTSServer:
  Unable to open socket for local`. Use `Xvfb :<n> -screen 0 640x480x24 -listen tcp -ac` with
  `DISPLAY=127.0.0.1:<n>`. Those `_XSERVTrans` lines on stderr are expected and harmless as long as
  the TCP listener comes up. Switching to `-nolisten tcp` or a plain unix-socket display breaks the
  suite outright.
- **Xvfb must be spawned by the same process tree that connects to it.** A server left running by an
  earlier shell invocation is not reachable from a later one; the connection is refused. So tests
  spawn Xvfb with `subprocess` from inside the test process, and a manual probe has to launch and
  use the server in a single command.
- **Xvfb leaves `/tmp/.X<n>-lock` behind when killed,** and the next start on that display number
  then dies with "Server is already active". Pick a free number by checking for the lock file, and
  unlink it on teardown.

## Architecture

Everything X-specific is confined to `macrorec/backend/x11.py`. Everything above it is pure and
testable with no display at all.

```
macrorec/
  events.py     event dataclasses, the one shared vocabulary; key/button aliases;
                the char-to-keysym-name map that `type` expands through
  script.py     the DSL parser and formatter. Pure, no X
  timeline.py   sleep deltas <-> absolute schedule, speed scaling
  collapse.py   motion reduction at record time: endpoints by default,
                interval-sampled path capture when the setting is on
  backend/
    base.py     abstract Recorder/Player. `Player.perform()` dispatches an event to
                seven primitives, so a backend only implements those seven
    x11.py      XRecord capture, XTEST injection, global hotkey grabs
    xi2.py      XI2Recorder (capture) and RawHotkeyWatch (the game-mode panic
                stop). Both see through a pointer/keyboard grab and a warp,
                which core XRecord/XGrabKey cannot - see the module docstring
    fake.py     in-memory backend for headless logic tests
  playback.py   runs a Schedule against a Player on a worker thread
  settings.py   JSON preferences under $XDG_CONFIG_HOME/macrorec/
  gui.py        PyQt5 window, a compact transport bar
```

Outside the package:

```
LAUNCH_macrorec_LinuxMac.sh   double-click launcher; finds Python 3, execs bootstrap.py
bootstrap.py                  builds the venv, installs deps, starts the app
requirements.txt              what bootstrap.py installs
```

Run it with `./LAUNCH_macrorec_LinuxMac.sh [file.macro]`, or during development with
`.venv/bin/python -m macrorec.gui [file.macro]`, or `macrorec` once pip-installed.

The `backend/base.py` seam exists so an evdev/uinput backend can be added later, covering Wayland or
a machine with `input` group membership, without the parser, timeline or GUI knowing about it.

## Things that are decisions, not accidents

- **Parsing normalises rather than preserves.** Aliases resolve (`ctrl` becomes `Control_L`, button
  `2` becomes `middle`), and comments other than a leading `# macro: <name>` are dropped. So the
  round-trip guarantee is `parse(format(m)) == m`, not byte equality.
- **Keysym names are stored, not keycodes.** XRecord yields keycodes and XTEST consumes them, but a
  hand-editable file needs names. The file header records the XKB layout so replay can warn on a
  mismatch instead of silently typing the wrong characters.
- **A key resolves to a keycode *and* a level.** `A` and `a` share a keycode and differ only by the
  modifier held, so `resolve_key()` returns both and the player holds Shift for level 1. Resolving
  to a keycode alone turns `type "Hi!"` into `hi1` with no error anywhere.
- **The recorder reports level-0 keysyms.** It writes `key a`, never `key A`; a held Shift is
  captured as its own event. Base keysyms plus modifier events reproduce what was typed, and the
  file stays readable.
- **Motion collapses to endpoints by default.** A run of pointer moves reduces to its last one,
  kept only if a mouse action follows. This is what keeps files editable. No `Sleep` is ever dropped
  by the reduction, so collapsing motion never changes when the next real action happens. That was
  only ever half the story, though, and the other half shipped broken: the sleeps have to *exist*
  first, and for fast mousing they did not - see the `to_events` bullet below.
- **A collapsed run's time is split around the surviving move: travel before it, dwell after.**
  Both halves are real - a user drags the pointer over, pauses, then clicks - and either extreme is a
  bug. All of it after the move hovers the target for the whole approach, firing tooltips and hover
  menus the recording never triggered. All of it before gives the target no hover at all and takes a
  drag's settle time away from the application, which needs a moment to process the motion before the
  button comes up. Splitting reproduces the original timing rather than redistributing it.
- **`merge_sleeps` runs last, over whatever the mode branch and the trims produced.** Recovering a
  fast stroke's time turns it into a long run of `sleep 5` lines, and one `sleep 594` says the same
  thing in a file whose whole point is being hand-editable. Order against the trims is immaterial:
  both pop a whole run of trailing sleeps, not one. It must not move into `script.parse`, though -
  two consecutive `sleep` lines in a hand-written file have to round-trip.
- **Path capture is the opt-in alternative, and it samples before `to_events`, not after.** The
  `capture_motion_path` setting swaps `collapse_motion` for `collapse.sample_motion`, which thins
  motion to one sample per `MOTION_SAMPLE_SECONDS` (16ms) and keeps the route. The ordering is the
  whole trick: `build_schedule` advances its clock only on a `Sleep`, so thinning *after* `to_events`
  throws away the sleeps standing between the moves it drops, and the stroke replays faster than it
  was made. The sample interval and `MIN_SLEEP_MS` are one knob seen twice; a test asserts 16 > 5 so
  nobody lowers the interval into the floor. Since sub-floor gaps are now deferred rather than
  discarded, the interval no longer decides whether a path keeps its *total* duration, only how
  finely that duration is written down - but the ordering rule stands unchanged.
- **`to_events` defers a sub-`MIN_SLEEP_MS` gap, it does not discard it.** The anchor stays put until
  a gap clears the floor, so the remainder folds into the next `Sleep`. Advancing it per event is
  what lost a whole fast mouse stroke in the default mode: raw XRecord motion arrives every 1-3ms, so
  a 0.6s drag was 300 separately-dropped gaps, zero sleeps, and a 0.000s schedule with everything
  after it firing early. Path capture never had it, because 16ms samples clear the floor - which is
  exactly why it stayed hidden until path capture was built. The anchor advances **by the amount
  emitted, never to the event's own timestamp**: resetting to `at` shaves the sub-millisecond
  fraction off once per sleep and that error random-walks across a long macro. Fixed 2026-08-23.
  **Measured against real hardware on `:0`, not just Xvfb:** a 4.9s freehand stroke delivered 2036
  motion samples at a median gap of **1.89ms**, with **1851 of 2035 gaps under the 5ms floor**. The
  schedule came out 3ms off the wall clock; before the fix it would have lost about 3.7s of 4.9s.
  Any test that spaces its motion above 5ms proves nothing about this - which is exactly how the
  defect survived a green suite for months.
- **A held-back sample is flushed before any non-move event.** Otherwise a click lands at a position
  up to one interval stale, which is the exact bug endpoint-collapse never had. The flush usually
  falls within `MIN_SLEEP_MS` of the previous sample, so both moves share a schedule offset. That is
  intended, not a rounding bug: a same-instant correction of a pixel or two buys an exact click. The
  gap is carried into the following sleep rather than lost, but it still gets no line of its own, so
  the shared offset is unchanged.
- **The player never sleeps for a delta.** It waits until `monotonic_base + step.at`, so a long loop
  does not accumulate drift. `Player.perform()` rejects `Sleep` outright to keep that honest.
- **The speed scalar divides every delay,** explicit `sleep` lines included. It scales the whole
  macro's tempo, which is the reading a user expects.
- **Recording delivers events off the calling thread.** The X recorder blocks in
  `record_enable_context()`, so events can only arrive after `start()` returns; `FakeRecorder`
  behaves the same way on purpose.
- **The Escape panic-stop cannot be a widget keybinding.** During playback the macro is driving some
  other application, so this tool's window does not have focus. It needs a server-side `XGrabKey` on
  the root window, taken when playback starts and released when it stops. The Record and Play
  hotkeys are global for the same reason, and are unbound by default because a grab takes that key
  away from every other program on the desktop.
- **Never grab with `X.AnyModifier`.** It looks like the way to catch a key whatever the lock state
  is, and it is a trap: the server refuses it wholesale with `BadAccess` if *any* combination
  involving that key is already grabbed by another client. MATE binds Alt+Escape to window
  switching, so an AnyModifier grab on Escape fails on an ordinary MATE desktop and the panic stop
  silently does nothing. Grab each combination in `LOCK_MASKS` instead. Measured 2026-08-21.
- **X reports a refused grab asynchronously, so `grab_key()` never raises.** Without an explicit
  `error.CatchError()` plus a `sync()`, a dead hotkey is indistinguishable from a working one. This
  is what hid the bug above: the GUI's "could not be armed" warning could not fire, because nothing
  ever threw.
- **Hotkeys are re-grabbed per mode, not held all at once.** `_bindings_for_mode()` is the single
  place that decides: panic only while playing, Record doubling as Stop while recording, Record and
  Play while idle. Arm the panic grab *before* the first event is injected.
- **A hotkey is a modifier mask plus a keysym, never a bare keysym.** `parse_hotkey()` turns
  `Ctrl+Shift+A` into both, and the grab is keyed on the pair, so two hotkeys can share a keycode.
  Lock bits are excluded from `MODIFIER_BITS` deliberately: CapsLock and NumLock must never decide
  whether a hotkey matches, which is also why every combination is grabbed once per `LOCK_MASKS`
  entry.
- **Upper case on a letter names the key; it does not request Shift.** `Ctrl+A` must fire on
  Ctrl+a, the way every toolkit spells it, so `parse_hotkey()` folds a single letter to its
  lower-case keysym and Shift has to be written out. Punctuation is the opposite case and keeps the
  level-based Shift, because `+` cannot be typed without it. Getting this backwards leaves `Ctrl+A`
  silently dead.
- **Trim the whole chord out of a recording, not just its last key.** A stop hotkey's modifier
  presses are recorded too, so `hotkey_syms()` names them and `_trim_trailing_key` removes them all.
- **No keybind is a literal at its call site.** Window shortcuts come from `settings` through
  `_apply_shortcuts()`, the same way global hotkeys come through `_rebind_hotkeys()`. A shortcut
  set with `setShortcut("Ctrl+O")` inline is one the settings dialog cannot list or change, and the
  failure is invisible because the key itself works. The two are still presented separately,
  because a global grab fires with any window focused while a window shortcut needs macrorec
  focused, and a global hotkey silently beats a window shortcut on the same combination.
- **A `QGroupBox` title wider than its box is clipped**, not wrapped, and not grown into. Keep them
  short; the long form belongs in a tooltip.
- **Only an unmodified panic key is withheld from playback.** A macro's plain Escape cannot trigger
  a `Ctrl+Escape` panic stop, so suppressing Escape in that case would break good macros for
  nothing. `panic_skip_sym()` is the one place that decides.
- **A fake must key on whatever the real backend keys on, and be no more permissive.** This has
  bitten three times: `FakeRecorder` delivered synchronously while XRecord can only deliver
  off-thread; `FakePlayer` ignored `skip_syms` while `X11Player` honours it; `FakeGrab` keyed on the
  hotkey text while `HotkeyGrab` keys on `(keycode, mask)`, so two spellings of one chord looked
  distinct in tests and collided in the app. Whenever a fake stores something the real one derives,
  ask what the derivation would have collapsed or rejected.
- **`setWindowFlag()` hides the widget**, because it re-parents it. So `isVisible()` immediately
  afterwards is always False, and a re-show guarded on it never runs, which makes the window vanish.
  Capture visibility *before* changing the flag. This is what made unchecking "on top" look like a
  crash.
- **Record and Play are mutually exclusive.** An active record context would otherwise capture the
  player's own injected events.
- **The window is a transport bar, not an editor.** Macro files are plain text, so editing belongs
  in whatever editor the user already has; Reload closes that loop. Record replaces the current
  macro with no prompt.
- **The GUI's backends are injectable.** `MacroRecWindow` takes `recorder_factory`,
  `player_factory`, `grab_factory`, `game_grab_factory` and `warnings_factory`, so the state
  machine is testable against fakes with no X server at all. `gui.py` imports Xlib inside
  functions, never at module scope.
- **Worker-thread callbacks reach the GUI through Qt signals** (`gui._Bridge`). Touching a widget
  from the playback or panic-grab thread is undefined behaviour.
- **The click that stops a recording is trimmed out of it.** XRecord taps every client, so pressing
  Stop is captured like any other click; left in, every macro would end by clicking wherever this
  window happened to be. Only a trailing interaction inside the window's own geometry, and only
  when the mouse was what stopped the recording, is treated as ours. With path capture on it is a
  whole run of moves walking here, not one, so the run goes too; the walk back stops at the first
  move outside our rect and at any event that is not a move or a sleep, which keeps it to the
  approach contiguous with that click. A macro that genuinely clicks over this window earlier on
  keeps that click and the path leading to it. **A stated restriction, not a silent gap: with
  `capture_raw_input` on, this scan always no-ops**, because game-mode recording emits `MoveRel`
  only - there is no absolute `Move` to find our window rect against. Recording is stopped with a
  hotkey in a fullscreen game anyway, where a click on our own Stop button is unreachable, so this
  is left as-is rather than built out.
- **`bootstrap.py` is stdlib-only and must stay that way.** It runs before python-xlib and PyQt5
  exist, so importing anything from `macrorec/` or a third-party package breaks the first launch,
  which is the launch where nothing else can help the user. A test enforces it.
- **`bootstrap.py` refuses a Wayland session with no X server** rather than letting Xlib fail with
  an error that explains nothing. XWayland warns and continues, because it half works: X11 clients
  are visible to it, native Wayland windows are not.
- **`LAUNCH_macrorec_LinuxMac.sh` must stay executable** (`100755`, matching the sibling projects).
  A test checks the bit, since git records it and a lost bit silently breaks double-click launching.
- **`Player` has a seventh primitive, `move_rel`.** `X11Player.move_rel` is XTEST with `detail=1`,
  which is what makes it relative rather than absolute like plain `move()` - measured against
  Xvfb: asked `(+25, -10)`, got exactly that, no evdev/uinput needed. A macro's `moverel` line only
  ever comes from a `capture_raw_input` recording, but replay does not care how a `MoveRel` was
  produced.
- **`capture_raw_input` swaps the recorder for `XI2Recorder` outright, not alongside `X11Recorder`.**
  Its `_finish_recording` branch also swaps `collapse_motion` for `accumulate_motion`, run before
  `to_events` like `sample_motion`: `collapse_motion` cannot touch `MoveRel` at all (collapsing
  deltas to their last one is meaningless) and `sample_motion` would keep-last instead of summing,
  silently discarding most of a fast turn.
- **The panic stop moves to `RawHotkeyWatch` only while `capture_raw_input` is on and only during
  playback**, never unconditionally. `HotkeyGrab`'s `XGrabKey` cannot survive a game's own
  exclusive keyboard grab, which is the whole reason this exists, but the swap is not free: XI2
  selection is passive observation, not a grab, so the panic key is no longer withheld from
  whatever window is focused - an `Escape` panic stop also opens the game's own menu. Tolerable
  because playback is already being aborted, and scoped to the one setting/mode where the
  trade is worth it; every other panic stop keeps `HotkeyGrab`'s exclusive behaviour unchanged.
  Record and Play hotkeys never move to XI2: they are pressed before a game has grabbed anything.
- **`RawHotkeyWatch` reconstructs a modifier chord from raw keycodes by hand**, since the raw
  payload carries no modifier field the way core events' `state` does (proven feasible, not built,
  by the M0 spike). It seeds held modifiers from `query_keymap()` at `start()` rather than assuming
  none are down - a Ctrl already held before playback begins must count from the first event, not
  only from a press the watcher happens to see afterwards. It tracks only the keycodes
  `MODIFIER_SYMS_BY_BIT` names, the same set `HotkeyGrab`'s `LOCK_MASKS` dance excludes CapsLock and
  NumLock from, so lock state never decides a match here either.
- **`RawHotkeyWatch` ignores its own XTEST-injected keys, by `sourceid`, and warn-and-skip drops
  away with `capture_raw_input` on because of it.** A macro genuinely containing the exact modified
  panic chord as ordinary content used to self-trigger the panic stop - measured, not assumed, in
  what is now `test_raw_hotkey_watch_does_not_self_trigger_on_a_replayed_panic_chord`'s inverse.
  `xtest_device_ids()` identifies the XTEST slave devices (by their `XTEST Device` property,
  falling back to a name match) and `RawHotkeyWatch` drops any raw event whose `sourceid` names one
  of them, before that event can enter held-modifier state or fire an action. Measured 2026-08-23:
  even the master-attributed echo of an ungrabbed XTEST injection carries the *slave's* `sourceid`,
  not the master's own, so the filter is correct ungrabbed as well as grabbed - see `xi2.py`'s
  module docstring for the numbers. **Confirmed on real hardware, not just Xvfb, 2026-08-26**: a
  live keypress still triggers the panic stop, and a macro replaying the same chord as ordinary
  content plays it back instead of self-triggering - checked with the panic hotkey rebound rather
  than at its Escape default, since the filter keys on device origin, not on which key was pressed,
  so any configured chord exercises the identical path. `HotkeyGrab`'s `XGrabKey` path gets none of this: core events
  carry nothing that distinguishes XTEST from a real key, so `panic_skip_sym`/warn-and-skip stay
  exactly as they were with `capture_raw_input` off. `macro_warnings(...,
  panic_key_is_withheld=False)` is what turns the warning off in the other mode, and
  `gui.start_playback` picks `skip=None` there instead of calling `panic_skip_sym`.
- **The filter fails closed, not open.** Discovering no XTEST devices, or `xtest_device_ids()`
  raising, makes `RawHotkeyWatch.start()` raise rather than arm unfiltered - an unfiltered watch
  would, once warn-and-skip is gone, let a replayed panic key stop the macro that legitimately
  contains it, which is worse than no panic stop at all. `gui._rebind_hotkeys()` already catches a
  `start()` failure and reports it, so the outcome is "no panic stop this run", not a silent,
  unfiltered watch. An *explicit* `injected_ids` (including `frozenset()`, what
  `tests/test_xi2_backend.py`'s `_hardware_watch` passes to stand in for real hardware, since Xvfb
  cannot produce a non-XTEST keypress) is used verbatim and skips discovery entirely.
- **`XI2Recorder` is deliberately not filtered.** Record and Play are mutually exclusive, so there
  is no self-capture to prevent, and every recorder test's stimulus *is* XTEST - filtering there
  would remove the only way to test the recorder headlessly.
