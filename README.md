# macrorec

Record what you type and click, replay it, loop it, and save it as a text file you can open and
edit by hand afterwards.

An AutoHotkey-style macro tool for X11, with the emphasis on macros being readable artifacts rather
than opaque recordings. A few seconds of mousing becomes half a dozen lines you can read, diff and
tweak in any editor.

X11 only, by design. See [Why X11 only](#why-x11-only) below.

## Install

Requires Python 3.11 or newer, an X11 session, and a server with the RECORD and XTEST extensions
(any ordinary Xorg has both). No root, and no group membership beyond a normal desktop login.

```
git clone <repository-url> macrorec
cd macrorec
./LAUNCH_macrorec_LinuxMac.sh
```

That is the whole install. The launcher creates a virtual environment, installs the dependencies,
and starts the program. It only downloads anything the first time. You can also double-click it
from a file manager.

<details>
<summary>Setting it up by hand instead</summary>

```
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m macrorec.gui
```

`--system-site-packages` lets the venv see a distribution-packaged PyQt5, which is how most Linux
distributions ship it (`python3-pyqt5` on Debian and Ubuntu, `python-pyqt5` on Arch). pip then
reports it as already satisfied and downloads nothing for it. Without that flag, PyQt5 is installed
from PyPI, which works but takes considerably longer.

</details>

**macOS note:** double-clicking a `.sh` file in Finder opens it in a text editor rather than running
it. Run `./LAUNCH_macrorec_LinuxMac.sh` from Terminal, or rename it to `.command`. You will also
need XQuartz, since macrorec is an X11 program.

## Quick usage

```
./LAUNCH_macrorec_LinuxMac.sh                # start
./LAUNCH_macrorec_LinuxMac.sh login.macro    # start with a file open
```

1. Press **Rec** and do the thing you want repeated. Press **Stop**.
2. Press **Play** to replay it. Set **Loop** to repeat, or to `0` to loop until stopped.
3. **Speed** scales the whole macro's tempo. `2.00x` runs it twice as fast.
4. **Save** writes a plain text file. Edit it in any editor, then **Reload**.

**Press Escape to stop a running macro.** It works even though the macro is driving some other
application, because macrorec takes a global grab on that key for the duration of playback. The
panic key is configurable under **Settings**.

**Settings** also offers optional global hotkeys for Record and Play, both unbound by default. Bind
one and you can start and stop a recording without coming back to the window, which is usually what
you want, since the thing you are recording is happening somewhere else. The Record hotkey stops the
take as well as starting it, and the keypress that ends a recording is not recorded.

They are unbound by default on purpose: a global grab takes that key away from every other program
for as long as macrorec is open, so it should be a key you chose. Function keys like `F9` and `F10`
are good candidates. If another program already holds the key you pick, macrorec says so rather than
leaving you with a hotkey that quietly does nothing.

Any hotkey may carry modifiers, which is usually the friendlier choice since it is far less likely
to collide with something else:

```
Escape            F9            Pause
Ctrl+Shift+A      Alt+F4        Super+r        Ctrl++
```

Modifiers are `Ctrl`, `Shift`, `Alt` and `Super` (`Control`, `Meta` and `Win` are accepted as
aliases). Case does not matter, and what you type is normalised, so `shift+ctrl+f9` is stored as
`Ctrl+Shift+F9`. CapsLock and NumLock never affect whether a hotkey fires.

Settings lists the window shortcuts too, and they are editable:

| Shortcut | Default |
| --- | --- |
| Open | `Ctrl+O` |
| Save | `Ctrl+S` |
| Save As | `Ctrl+Shift+S` |
| Reload | `Ctrl+R` |

The two groups are separated because they work differently. A **global hotkey** is a grab held on
the X server, so it fires whatever window has focus. A **window shortcut** is an ordinary keybinding
that only works while macrorec is focused, which is why these can safely default to the conventional
combinations. If you set a global hotkey to the same combination as a window shortcut, macrorec
refuses it: the grab intercepts the key before the window ever sees it, so the shortcut would simply
never fire.

One consequence worth knowing: a macro containing the panic key has those keystrokes skipped during
playback, since otherwise it would stop itself. That only applies when the panic key has no
modifiers. With `Ctrl+Escape` as the panic stop, a macro's plain `Escape` cannot trigger it, so it
is typed normally. This does not apply with **Capture raw input** on, the Settings checkbox for
recording mouselook in a fullscreen game: its panic stop tells its own keystrokes from real ones,
so nothing is skipped and there is nothing to warn about.

Recording captures keystrokes, clicks, scrolling, and the pointer position where each click
happens. Pointer movement between clicks is not recorded, which is what keeps the files short and
editable.

If you need the route the pointer took rather than just where it stopped, turn on **Capture mouse
movement paths** under Recording in the settings dialog. Drags and freehand strokes then replay
along the same path at the same speed. It is off by default, because it makes macro files far
longer and harder to edit by hand, and it applies to the next recording you start.

## Macro file format

```
# macro: login-sequence
version 1
layout us

key Return
sleep 250ms
type "hello world"
sleep 1s
move 640 400
click left
sleep 500ms
keydown ctrl
key s
keyup ctrl
scroll up 3
```

One command per line. `#` starts a comment, which runs to the end of the line unless it falls
inside a quoted string.

| Command | Meaning |
| --- | --- |
| `key <name>` | Press and release one key |
| `keydown <name>` / `keyup <name>` | Hold a key down, release it later |
| `type "<text>"` | Type a string. Expands to individual keystrokes at replay time |
| `move <x> <y>` | Move the pointer to an absolute screen position |
| `moverel <dx> <dy>` | Move the pointer by a relative offset, signed integers |
| `click <left\|middle\|right>` | Press and release a button |
| `mousedown <button>` / `mouseup <button>` | Hold a button, for dragging |
| `scroll <up\|down\|left\|right> [count]` | Scroll, `count` detents (default 1) |
| `sleep <n>ms` / `sleep <n>s` | Wait |

Header directives, all optional, all before the first command:

| Directive | Meaning |
| --- | --- |
| `version 1` | File format version |
| `layout <name>` | Keyboard layout it was recorded on. Replay warns if yours differs |
| `speed <n>` | Starting value for the speed control |

Key names are X keysym names: `Return`, `Escape`, `F5`, `Left`, `space`, `exclam`. A few friendly
aliases are accepted and normalised on load, so `ctrl` becomes `Control_L` when the file is next
saved: `ctrl`, `alt`, `shift`, `super`, `esc`, `enter`, `del`, `ins`, `pgup`, `pgdn`. Buttons may
be written as `1`, `2`, `3` instead of `left`, `middle`, `right`.

Recording writes unshifted key names and records the Shift as its own `keydown`/`keyup` pair, so
you will see `keydown shift` / `key a` rather than `key A`. Both forms replay correctly, and
writing `key A` or `type "Hi"` by hand does the right thing.

## Notes on behaviour

- **Speed divides every delay, `sleep` lines included.** It scales the macro's tempo rather than
  only the gaps the recorder left.
- **Timing does not drift.** Delays are converted to offsets from a single start instant, so a
  macro looped a hundred times ends on schedule rather than a hundred roundings late.
- **Mouse motion is recorded as endpoints, not as a path**, so a few seconds of mousing becomes one
  `move` line instead of thousands. The time it took is kept: you get the travel as a `sleep` before
  the move and any pause on the target as a `sleep` after it. Turn on **Capture motion path** in
  Settings when the route itself matters, for freehand drawing or a drag that has to follow a
  particular line.
- **Record and Play are mutually exclusive.** Recording during playback would capture macrorec's
  own injected input.
- **A macro that contains the panic key still plays**, with those keystrokes skipped and a warning
  when it loads. Otherwise it would stop itself. With **Capture raw input** on, neither the skip nor
  the warning applies: the panic watch tells its own keystrokes from real ones and the panic key
  types normally.
- **The click on Stop that ends a recording is not recorded.** Only that final click, and only when
  the mouse is what stopped the recording. An earlier click on the macrorec window during a
  recording is kept, on the grounds that it might have been deliberate.
- Preferences live in `$XDG_CONFIG_HOME/macrorec/settings.json`, or `~/.config/macrorec/`.
- **`moverel <dx> <dy>` replays a relative pointer move**, the form **Capture raw input** records
  instead of absolute positions, since a fullscreen game's own pointer grab and warp-to-centre hide
  the real motion from ordinary recording.

## Why X11 only

A macro recorder cannot be an ordinary Wayland client. The compositor delivers input only to the
focused surface, and no protocol lets a normal client observe input globally. That is a deliberate
security property of Wayland, not a gap waiting to be filled. The two ways around it both cost
something:

- **evdev/uinput**, reading `/dev/input/event*` and injecting through `/dev/uinput`, bypassing the
  display server. Works under both X11 and Wayland, which is how `ydotool` does it. It needs
  `input` group membership plus a uinput udev rule, and it yields raw keycodes and relative pointer
  deltas rather than keysym names and absolute positions.
- **Portals**, `org.freedesktop.portal.RemoteDesktop` for injection and `InputCapture` for capture.
  The sanctioned route, but `InputCapture` needs a portal backend built against libei, which many
  distributions do not ship yet.

X11 via XRecord and XTEST needs no privileges, gives absolute positions and keysym names, and can
be tested headlessly. `macrorec/backend/base.py` is an abstract Recorder/Player pair with exactly
that future in mind: an evdev backend can be added behind it without the parser, the timeline or
the GUI knowing.

## Development

```
.venv/bin/python -m pytest
```

Dependencies are listed in both `pyproject.toml` and `requirements.txt`. `bootstrap.py` is
stdlib-only, since it runs before anything is installed, so it cannot read the project's own
metadata to find them. A test fails if the two lists disagree.

The core (`events`, `script`, `timeline`, `collapse`, `playback`, `settings`) is pure Python and
tested without a display. The X backend and GUI tests spawn a headless Xvfb automatically, and skip
rather than fail if `Xvfb` or `python-xlib` is missing.

A few tests spawn a second headless display running a real window manager (`marco`), because
keyboard grabs behave differently once something else on the desktop is holding bindings of its
own. Those skip if `marco` is not installed.

```
LAUNCH_macrorec_LinuxMac.sh   double-click launcher, hands off to bootstrap.py
bootstrap.py                  builds the venv, installs deps, starts the app
macrorec/
  events.py     event types, the shared vocabulary
  script.py     the DSL parser and formatter
  timeline.py   sleep deltas to an absolute schedule, speed scaling
  collapse.py   pointer-motion reduction at record time, endpoints or sampled path
  playback.py   runs a schedule against a player, on a worker thread
  settings.py   JSON preferences
  backend/
    base.py     abstract Recorder/Player
    x11.py      XRecord capture, XTEST injection, global hotkey grabs
    fake.py     in-memory backend for tests
  gui.py        PyQt5 window
```

### If Xvfb will not start

If your test environment's Xvfb cannot open its unix socket, run it over TCP loopback instead:
`Xvfb :99 -screen 0 640x480x24 -listen tcp -ac` with `DISPLAY=127.0.0.1:99`. Spawn it from the same
process that connects to it, and remove `/tmp/.X99-lock` afterwards if Xvfb was killed rather than
asked to exit.

## License

Copyright (C) 2026 Combinebobnt

GPL-3.0-or-later; see `LICENSE`. This program is free software: you can redistribute it and modify
it under the terms of the GNU General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version. It is distributed in the
hope that it will be useful, but WITHOUT ANY WARRANTY, without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.

PyQt5 is GPLv3, so this is the consistent choice. python-xlib is LGPL-2.1-or-later and keeps its
own terms.
