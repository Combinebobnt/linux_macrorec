#!/usr/bin/env python3
"""Wayland/KWin go/no-go spike for a hypothetical evdev/uinput backend in linux_macrorec.

Superseded `wayland_m0_spike.py`, which used macrorec's own X11Recorder as the observing oracle -
that only works on an X11 desktop. This script targets a real Wayland session (KDE Plasma/KWin)
run by someone else, in one sitting, with no macrorec import at all: stdlib + evdev + PyQt.

Full rationale: ~/.claude/plans/2026-09-03-linux-macrorec-wayland-kwin-spike.md

Run it on the real desktop, in a real terminal - a Claude Code session's sandbox stubs /dev and
has no compositor, so this cannot be run from inside one.

First time on this machine:
    python3 wayland_kwin_spike.py --setup
Then, after following that block (packages, privileges, a log out/in):
    python3 wayland_kwin_spike.py
Paste the whole printed VERDICT block back.

Optional override if keyboard auto-detection picks the wrong node:
    MACROREC_SPIKE_KBD=/dev/input/eventN python3 wayland_kwin_spike.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve()

PASS_MARKER = "##PASS-JSON##"
CHILD_TIMEOUT_S = 150
CHILD_HARD_QUIT_S = 130

CLICK_TIMEOUT_S = 60
JIGGLE_TIMEOUT_S = 5.0
JIGGLE_INTERVAL_S = 0.05
GATE_SETTLE_S = 0.15
CAPTURE_SECONDS = 8
CAPTURE_PROMPT = "the quick fox"

REL_INJECT_DX, REL_INJECT_DY = 100, 100
CORNER_PIN_MAGNITUDE = 100_000
R2C_STEP = 20
R2C_STEPS = 15
R2_TOLERANCE_PX = 2

REPORTED_ENV = (
    "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP",
    "WAYLAND_DISPLAY", "DISPLAY", "QT_QPA_PLATFORM", "KDE_SESSION_VERSION",
)

_PSEUDO_DEVICE_NAMES = re.compile(
    r"power button|video bus|pc speaker|sleep button|led controller|hdmi",
    re.IGNORECASE,
)


def _fail_import(what: str, exc: Exception) -> None:
    print(f"Cannot import {what}: {exc}")
    print("Install it (see --setup for the distro-specific package name) and re-run.")
    sys.exit(1)


try:
    import evdev
    from evdev import AbsInfo, InputDevice, UInput, ecodes as e
except ImportError as exc:
    _fail_import("evdev", exc)


# --- env reporting, allowlisted ---------------------------------------------

def report_env() -> None:
    print("\n=== Environment (allowlisted) ===")
    for name in REPORTED_ENV:
        print(f"  {name}={os.environ.get(name)!r}")


# --- distro detection + --setup ---------------------------------------------

def detect_distro_id() -> str:
    try:
        text = Path("/etc/os-release").read_text()
    except OSError:
        return "unknown"
    for line in text.splitlines():
        if line.startswith("ID="):
            return line.split("=", 1)[1].strip().strip('"').lower()
    return "unknown"


_SETUP_BLOCKS = {
    "fedora": """\
sudo modprobe uinput
echo uinput | sudo tee /etc/modules-load.d/uinput.conf
echo 'KERNEL=="uinput", GROUP="input", MODE="0660"' | sudo tee /etc/udev/rules.d/99-uinput.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo usermod -aG input $USER
sudo dnf install -y python3-evdev python3-pyqt5 qt5-qtwayland
# log out and back in, then re-run this script with no arguments""",
    "arch": """\
sudo modprobe uinput
echo uinput | sudo tee /etc/modules-load.d/uinput.conf
echo 'KERNEL=="uinput", GROUP="input", MODE="0660"' | sudo tee /etc/udev/rules.d/99-uinput.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo usermod -aG input $USER
sudo pacman -S --needed python-evdev python-pyqt5 qt5-wayland
# log out and back in, then re-run this script with no arguments""",
    "debian": """\
sudo modprobe uinput
echo uinput | sudo tee /etc/modules-load.d/uinput.conf
echo 'KERNEL=="uinput", GROUP="input", MODE="0660"' | sudo tee /etc/udev/rules.d/99-uinput.rules
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo usermod -aG input $USER
sudo apt install -y python3-evdev python3-pyqt5 qtwayland5
# log out and back in, then re-run this script with no arguments""",
}
_SETUP_BLOCKS["ubuntu"] = _SETUP_BLOCKS["debian"]

_UNDO_BLOCKS = {
    "fedora": "sudo gpasswd -d $USER input\n"
              "sudo rm /etc/udev/rules.d/99-uinput.rules /etc/modules-load.d/uinput.conf\n"
              "sudo udevadm control --reload-rules\n"
              "# log out and back in for the group change to take effect",
}
_UNDO_BLOCKS["arch"] = _UNDO_BLOCKS["fedora"]
_UNDO_BLOCKS["debian"] = _UNDO_BLOCKS["fedora"]
_UNDO_BLOCKS["ubuntu"] = _UNDO_BLOCKS["fedora"]


def print_setup_block() -> None:
    distro = detect_distro_id()
    block = _SETUP_BLOCKS.get(distro)
    print(f"Detected distro: {distro}")
    if block is None:
        print("Unrecognised distro - do this by hand:")
        print("  1. modprobe uinput, and load it at boot (modules-load.d)")
        print("  2. a udev rule making /dev/uinput group input, mode 0660")
        print("  3. usermod -aG input $USER")
        print("  4. install your distro's evdev, PyQt5 (or PyQt6), and Qt Wayland platform "
              "plugin packages")
        print("  5. log out and back in (newgrp is not enough)")
        return
    print("\nRun this once, before the first spike run:\n")
    print(block)
    print(f"\nTo undo these grants afterwards:\n\n{_UNDO_BLOCKS[distro]}")
    print("\n--sudo -E is a fallback if you would rather not make these changes permanent, "
          "but Qt-as-root under Wayland needs XDG_RUNTIME_DIR/WAYLAND_DISPLAY preserved and "
          "will print warnings.")


# --- privilege + device census ------------------------------------------------

def parse_proc_input_devices() -> list[dict]:
    text = Path("/proc/bus/input/devices").read_text()
    blocks = []
    for chunk in text.split("\n\n"):
        info: dict = {"Handlers": ""}
        for line in chunk.splitlines():
            if line.startswith("N: Name="):
                info["Name"] = line.split("=", 1)[1].strip('"')
            elif line.startswith("H: Handlers="):
                info["Handlers"] = line.split("=", 1)[1].strip()
        if info.get("Name"):
            blocks.append(info)
    return blocks


def find_keyboard_node() -> tuple[str | None, bool]:
    """Returns (node, in_input_group). in_input_group is inferred from read access
    to a real keyboard node, the same signal the census in the old spike used."""
    override = os.environ.get("MACROREC_SPIKE_KBD")
    if override:
        return override, os.access(override, os.R_OK)

    candidates = []
    for dev in parse_proc_input_devices():
        m = re.search(r"event(\d+)", dev["Handlers"])
        if not m or "kbd" not in dev["Handlers"] or "js" in dev["Handlers"].split():
            continue
        if _PSEUDO_DEVICE_NAMES.search(dev["Name"]):
            continue
        if dev["Name"].startswith("macrorec-spike-"):
            continue  # our own virtual devices from an earlier gate in this same run
        node = f"/dev/input/event{m.group(1)}"
        candidates.append((node, os.access(node, os.R_OK)))

    readable = [n for n, ok in candidates if ok]
    if readable:
        return readable[0], True
    return (candidates[0][0] if candidates else None), False


def wait_for_device(name: str, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(d["Name"] == name for d in parse_proc_input_devices()):
            return
        time.sleep(0.02)
    raise TimeoutError(f"uinput device {name!r} never appeared in /proc/bus/input/devices")


# --- P1: grab + self-filter, own virtual device only, informational ---------

def run_p1(uinput_writable: bool, in_input_group: bool) -> dict:
    if not uinput_writable or not in_input_group:
        return {"pass": None, "note": "skipped: needs uinput write + input group "
                                       "(reading back our own eventN node needs the group too)"}
    try:
        cap = {e.EV_KEY: [e.KEY_K]}
        with UInput(cap, name="macrorec-spike-p1") as dev:
            wait_for_device("macrorec-spike-p1")
            # UInput doesn't expose its own /dev/input/eventN path directly; find it by name.
            node = next(
                (f"/dev/input/event{m.group(1)}"
                 for d in parse_proc_input_devices()
                 if d["Name"] == "macrorec-spike-p1"
                 for m in [re.search(r"event(\d+)", d["Handlers"])] if m),
                None)
            if node is None:
                return {"pass": None, "note": "could not find the uinput device's own event node"}

            observer = InputDevice(node)
            grabber = InputDevice(node)
            try:
                def tap() -> None:
                    dev.write(e.EV_KEY, e.KEY_K, 1)
                    dev.write(e.EV_KEY, e.KEY_K, 0)
                    dev.syn()

                def observed_within(timeout: float) -> bool:
                    r, _, _ = select.select([observer.fd], [], [], timeout)
                    if not r:
                        return False
                    return any(ev.type == e.EV_KEY for ev in observer.read())

                tap()
                baseline = observed_within(0.3)

                grabber.grab()
                tap()
                seen_while_grabbed = observed_within(0.3)
                grabber.ungrab()

                tap()
                seen_after_release = observed_within(0.3)

                ok = baseline and not seen_while_grabbed and seen_after_release
                return {"pass": ok, "baseline": baseline,
                        "seen_while_grabbed": seen_while_grabbed,
                        "seen_after_release": seen_after_release}
            finally:
                try:
                    grabber.ungrab()
                except OSError:
                    pass
                observer.close()
                grabber.close()
    except Exception as exc:  # noqa: BLE001 - reported, never fatal to the run
        return {"pass": None, "note": f"error: {exc}"}


# --- child pass: the Qt oracle + injection gates -----------------------------

def _import_qt():
    try:
        from PyQt5 import QtCore, QtGui, QtWidgets
        return "PyQt5", QtCore, QtGui, QtWidgets
    except ImportError:
        pass
    try:
        from PyQt6 import QtCore, QtGui, QtWidgets
        return "PyQt6", QtCore, QtGui, QtWidgets
    except ImportError as exc:
        _fail_import("PyQt5 or PyQt6", exc)


def enumerate_qt_platform_plugins() -> list[str]:
    """No QApplication needed - forcing QT_QPA_PLATFORM with no matching plugin makes Qt
    qFatal() and kill the process, so this has to run before construction, not after."""
    try:
        from PyQt5 import QtCore
        binding = "PyQt5"
    except ImportError:
        try:
            from PyQt6 import QtCore
            binding = "PyQt6"
        except ImportError:
            return []
    try:
        if binding == "PyQt5":
            plugins_dir = QtCore.QLibraryInfo.location(QtCore.QLibraryInfo.PluginsPath)
        else:
            plugins_dir = QtCore.QLibraryInfo.path(QtCore.QLibraryInfo.LibraryPath.PluginsPath)
        return sorted(os.listdir(os.path.join(plugins_dir, "platforms")))
    except OSError:
        return []


def make_oracle_class(QtWidgets, record, focus_confirmed, abort_requested):
    """Factory so the Xvfb/XTEST verification harness can drive the exact same event
    handlers run_pass() uses, without needing the uinput-dependent gate thread around it."""

    class Oracle(QtWidgets.QWidget):
        def __init__(self) -> None:
            super().__init__()
            self.setMouseTracking(True)
            self.label = QtWidgets.QLabel(
                "Click anywhere in this window to begin.\n"
                "Do not move the mouse or type until told to.", self)
            self.label.move(40, 40)
            self.label.setStyleSheet("font-size: 18px; color: white; background: black;")
            self.label.adjustSize()

        def mousePressEvent(self, ev) -> None:
            pos = ev.position() if hasattr(ev, "position") else ev.pos()
            record("click", pos.x(), pos.y())
            if not focus_confirmed.is_set():
                self.label.setText("Recording. Leave this window focused.")
                self.label.adjustSize()
                focus_confirmed.set()

        def mouseMoveEvent(self, ev) -> None:
            pos = ev.position() if hasattr(ev, "position") else ev.pos()
            record("move", pos.x(), pos.y())

        def mouseReleaseEvent(self, ev) -> None:
            pos = ev.position() if hasattr(ev, "position") else ev.pos()
            record("release", pos.x(), pos.y())

        def wheelEvent(self, ev) -> None:
            record("wheel", ev.angleDelta().y())

        def keyPressEvent(self, ev) -> None:
            record("key", ev.text())

        def tabletEvent(self, ev) -> None:
            pos = ev.position() if hasattr(ev, "position") else ev.posF()
            record("tablet", pos.x(), pos.y())

        def focusOutEvent(self, ev) -> None:
            record("focus_out")
            if focus_confirmed.is_set():
                abort_requested.set()

        def closeEvent(self, ev) -> None:
            abort_requested.set()

    return Oracle


def run_pass(platform: str) -> dict:
    """Runs entirely in a child process with QT_QPA_PLATFORM already set in its env.
    Prints exactly one PASS_MARKER line to stdout; everything else goes to stderr so
    the parent never has to relay (and risk leaking env from) this process's chatter."""
    binding, QtCore, QtGui, QtWidgets = _import_qt()

    app = QtWidgets.QApplication(sys.argv[:1])
    platform_name = app.platformName()
    print(f"[{platform}] Qt binding={binding} platformName()={platform_name!r}", file=sys.stderr)

    screens = QtGui.QGuiApplication.screens()
    screen_geoms = [{"x": s.geometry().x(), "y": s.geometry().y(),
                      "w": s.geometry().width(), "h": s.geometry().height()} for s in screens]

    report: dict = {
        "platform": platform, "platform_name": platform_name,
        "binding": binding, "screens": screen_geoms,
        "click_timeout": False, "aborted": False,
        "gates": {}, "c1": {"pass": None, "note": "not reached"},
    }

    observations: list[tuple] = []
    obs_lock = threading.Lock()
    focus_confirmed = threading.Event()
    abort_requested = threading.Event()
    gates_done = threading.Event()

    def record(kind: str, *fields) -> None:
        with obs_lock:
            observations.append((time.monotonic(), kind, *fields))

    def local_pos_since(since: int) -> tuple[float, float] | None:
        """Only looks at observations recorded after index `since`. Scanning the whole
        history would find a stale move/tablet entry left over from an *earlier* gate when
        the device just injected produced no event at all, misreporting "not observed" as
        "missed by N px" - exactly the distinction the multi-output verdict override needs."""
        with obs_lock:
            for entry in reversed(observations[since:]):
                if entry[1] in ("move", "tablet"):
                    return entry[2], entry[3]
        return None

    Oracle = make_oracle_class(QtWidgets, record, focus_confirmed, abort_requested)
    window = Oracle()
    window.showFullScreen()

    def gate_thread() -> None:
        if not focus_confirmed.wait(CLICK_TIMEOUT_S):
            report["click_timeout"] = True
            gates_done.set()
            return

        try:
            run_gates()
        finally:
            report["aborted"] = abort_requested.is_set()
            gates_done.set()

    def run_gates() -> None:
        gates = report["gates"]
        screen0 = screens[0].geometry()
        target = (int(screen0.width() * 0.75), int(screen0.height() * 0.33))

        relkbd_cap = {
            e.EV_KEY: [e.KEY_K, e.BTN_LEFT],
            e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL],
        }
        with UInput(relkbd_cap, name="macrorec-spike-relkbd") as relkbd:
            wait_for_device("macrorec-spike-relkbd")

            # --- A1: acquisition handshake ---
            a1_elapsed = None
            a1_ok = False
            deadline = time.monotonic() + JIGGLE_TIMEOUT_S
            start = time.monotonic()
            before = len(observations)
            while time.monotonic() < deadline and not abort_requested.is_set():
                relkbd.write(e.EV_REL, e.REL_X, 1)
                relkbd.write(e.EV_REL, e.REL_Y, 1)
                relkbd.syn()
                time.sleep(JIGGLE_INTERVAL_S / 2)
                relkbd.write(e.EV_REL, e.REL_X, -1)
                relkbd.write(e.EV_REL, e.REL_Y, -1)
                relkbd.syn()
                time.sleep(JIGGLE_INTERVAL_S / 2)
                with obs_lock:
                    a1_ok = any(o[1] == "move" for o in observations[before:])
                if a1_ok:
                    a1_elapsed = time.monotonic() - start
                    break
            gates["A1"] = {"pass": a1_ok, "elapsed_s": a1_elapsed}
            if not a1_ok or abort_requested.is_set():
                return

            # --- R1: key / click / wheel ---
            time.sleep(GATE_SETTLE_S)
            before = len(observations)
            relkbd.write(e.EV_KEY, e.KEY_K, 1); relkbd.syn()
            time.sleep(0.02)
            relkbd.write(e.EV_KEY, e.KEY_K, 0); relkbd.syn()
            time.sleep(GATE_SETTLE_S)
            relkbd.write(e.EV_KEY, e.BTN_LEFT, 1); relkbd.syn()
            time.sleep(0.02)
            relkbd.write(e.EV_KEY, e.BTN_LEFT, 0); relkbd.syn()
            time.sleep(GATE_SETTLE_S)
            relkbd.write(e.EV_REL, e.REL_WHEEL, 1); relkbd.syn()
            time.sleep(GATE_SETTLE_S)
            with obs_lock:
                new = observations[before:]
            key_ok = any(o[1] == "key" and o[2].lower() == "k" for o in new)
            click_ok = any(o[1] == "click" for o in new)
            wheel_ok = any(o[1] == "wheel" for o in new)
            gates["R1"] = {"pass": key_ok and click_ok and wheel_ok,
                            "key": key_ok, "click": click_ok, "wheel": wheel_ok}
            if not gates["R1"]["pass"] or abort_requested.is_set():
                return

            # --- R2c: relative corner-pin, informational only ---
            try:
                before = len(observations)
                relkbd.write(e.EV_REL, e.REL_X, CORNER_PIN_MAGNITUDE)
                relkbd.write(e.EV_REL, e.REL_Y, CORNER_PIN_MAGNITUDE)
                relkbd.syn()
                time.sleep(GATE_SETTLE_S)
                r2c_target = (screen0.width() - 1 - 300, screen0.height() - 1 - 300)
                steps = max(1, 300 // R2C_STEP)
                for _ in range(steps):
                    relkbd.write(e.EV_REL, e.REL_X, -R2C_STEP)
                    relkbd.write(e.EV_REL, e.REL_Y, -R2C_STEP)
                    relkbd.syn()
                    time.sleep(0.01)
                time.sleep(GATE_SETTLE_S)
                pos = local_pos_since(before)
                if pos is None:
                    gates["R2c"] = {"landing_error_px": None,
                                     "note": "not observed - may be off-output"}
                else:
                    err = ((pos[0] - r2c_target[0]) ** 2 + (pos[1] - r2c_target[1]) ** 2) ** 0.5
                    gates["R2c"] = {"landing_error_px": round(err, 1), "local": list(pos),
                                     "target": list(r2c_target)}
            except Exception as exc:  # noqa: BLE001
                gates["R2c"] = {"landing_error_px": None, "note": f"error: {exc}"}

            # --- Rrel: relative motion ratio, informational only ---
            try:
                before_pin = len(observations)
                relkbd.write(e.EV_REL, e.REL_X, CORNER_PIN_MAGNITUDE)
                relkbd.write(e.EV_REL, e.REL_Y, CORNER_PIN_MAGNITUDE)
                relkbd.syn()
                time.sleep(GATE_SETTLE_S)
                p0 = local_pos_since(before_pin)
                before_move = len(observations)
                relkbd.write(e.EV_REL, e.REL_X, REL_INJECT_DX)
                relkbd.write(e.EV_REL, e.REL_Y, REL_INJECT_DY)
                relkbd.syn()
                time.sleep(GATE_SETTLE_S)
                p1 = local_pos_since(before_move)
                pointer_speed = _read_pointer_speed()
                if p0 is None or p1 is None:
                    gates["Rrel"] = {"note": "not observed - may be off-output",
                                      "pointer_speed": pointer_speed}
                else:
                    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
                    gates["Rrel"] = {
                        "injected": [REL_INJECT_DX, REL_INJECT_DY],
                        "observed": [round(dx, 1), round(dy, 1)],
                        "ratio": [round(dx / REL_INJECT_DX, 2) if REL_INJECT_DX else None,
                                  round(dy / REL_INJECT_DY, 2) if REL_INJECT_DY else None],
                        "pointer_speed": pointer_speed,
                    }
            except Exception as exc:  # noqa: BLE001
                gates["Rrel"] = {"note": f"error: {exc}"}

            if abort_requested.is_set():
                return

            # --- R2a: plain absolute pointer device ---
            gates["R2a"] = _run_abs_gate(
                "macrorec-spike-abs", target, screen0, tablet=False)
            if abort_requested.is_set():
                return

            # --- R2b: tablet-style absolute device ---
            gates["R2b"] = _run_abs_gate(
                "macrorec-spike-tablet", target, screen0, tablet=True)
            if abort_requested.is_set():
                return

        # --- C1: capture round-trip, real keyboard, oracle still focused ---
        report["c1"] = _run_c1()

    def _run_abs_gate(name: str, target: tuple[int, int], screen0, tablet: bool) -> dict:
        try:
            if tablet:
                cap = {
                    e.EV_KEY: [e.BTN_TOOL_PEN, e.BTN_TOUCH],
                    e.EV_ABS: [
                        (e.ABS_X, AbsInfo(0, 0, screen0.width() - 1, 0, 0, 1000)),
                        (e.ABS_Y, AbsInfo(0, 0, screen0.height() - 1, 0, 0, 1000)),
                    ],
                }
                kwargs = {"input_props": [e.INPUT_PROP_DIRECT]}
            else:
                cap = {
                    e.EV_KEY: [e.BTN_LEFT],
                    e.EV_ABS: [
                        (e.ABS_X, AbsInfo(0, 0, screen0.width() - 1, 0, 0, 0)),
                        (e.ABS_Y, AbsInfo(0, 0, screen0.height() - 1, 0, 0, 0)),
                    ],
                }
                kwargs = {}
            with UInput(cap, name=name, **kwargs) as dev:
                wait_for_device(name)
                time.sleep(GATE_SETTLE_S)
                before = len(observations)
                dev.write(e.EV_ABS, e.ABS_X, target[0])
                dev.write(e.EV_ABS, e.ABS_Y, target[1])
                if tablet:
                    dev.write(e.EV_KEY, e.BTN_TOOL_PEN, 1)
                dev.syn()
                time.sleep(GATE_SETTLE_S)
                if tablet:
                    dev.write(e.EV_KEY, e.BTN_TOOL_PEN, 0)
                    dev.syn()
            pos = local_pos_since(before)
            if pos is None:
                return {"pass": False, "status": "not_observed",
                        "note": "not observed - may be off-output", "target": list(target)}
            within = abs(pos[0] - target[0]) <= R2_TOLERANCE_PX and \
                abs(pos[1] - target[1]) <= R2_TOLERANCE_PX
            return {"pass": within, "status": "pass" if within else "missed",
                    "local": [round(pos[0], 1), round(pos[1], 1)], "target": list(target)}
        except Exception as exc:  # noqa: BLE001 - reported, never fatal to the run
            return {"pass": False, "status": "error", "note": str(exc)}

    def _run_c1() -> dict:
        node, in_group = find_keyboard_node()
        if node is None or not in_group:
            return {"pass": None, "in_input_group": in_group,
                     "note": "skipped: no readable keyboard node "
                             "(input group grant not active yet)"}
        print(f'Type this now, lowercase, then wait: "{CAPTURE_PROMPT}"', file=sys.stderr)
        dev = InputDevice(node)
        typed = []
        any_key_event = False
        try:
            deadline = time.monotonic() + CAPTURE_SECONDS
            while time.monotonic() < deadline:
                remaining = max(0.0, deadline - time.monotonic())
                r, _, _ = select.select([dev.fd], [], [], remaining)
                if not r:
                    break
                for ev in dev.read():
                    if ev.type != e.EV_KEY or ev.value != 1:
                        continue
                    any_key_event = True
                    name = e.KEY.get(ev.code)
                    if not name:
                        continue
                    name = name[4:] if name.startswith("KEY_") else name
                    if name == "SPACE":
                        typed.append(" ")
                    elif len(name) == 1:
                        typed.append(name.lower())
        finally:
            dev.close()
        got = "".join(typed)
        with obs_lock:
            oracle_key_count = sum(1 for o in observations if o[1] == "key")
        if not any_key_event:
            return {"pass": False, "in_input_group": in_group, "observed": "", "node": node,
                     "oracle_key_events_seen": oracle_key_count,
                     "note": "zero key events on this node during the capture window - likely "
                             "the wrong node was auto-detected, not a capture failure; re-run "
                             f"with MACROREC_SPIKE_KBD=<the right /dev/input/eventN>"}
        return {"pass": got.strip() == CAPTURE_PROMPT, "in_input_group": in_group,
                "observed": got, "expected": CAPTURE_PROMPT, "node": node,
                "oracle_key_events_seen": oracle_key_count}

    thread = threading.Thread(target=gate_thread, daemon=True)
    thread.start()

    exec_method = getattr(app, "exec", None) or app.exec_
    quit_timer = QtCore.QTimer()
    quit_timer.setSingleShot(True)
    quit_timer.timeout.connect(app.quit)
    quit_timer.start(int(CHILD_HARD_QUIT_S * 1000))

    poll_timer = QtCore.QTimer()
    poll_timer.timeout.connect(lambda: app.quit() if gates_done.is_set() else None)
    poll_timer.start(100)

    exec_method()
    thread.join(timeout=2)

    print(f"{PASS_MARKER}{json.dumps(report)}")
    return report


def _read_pointer_speed() -> str:
    for tool in ("kreadconfig6", "kreadconfig5"):
        try:
            out = subprocess.run(
                [tool, "--file", "kcminputrc", "--group", "Mouse", "--key", "XLbInptAccelSpeed"],
                capture_output=True, text=True, timeout=2)
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            continue
    return "unknown (not queryable from this script)"


# --- parent orchestration -----------------------------------------------------

def run_child_pass(platform: str) -> dict:
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = platform
    try:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--child-pass", platform],
            env=env, stdout=subprocess.PIPE, text=True, timeout=CHILD_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"error": "the oracle window pass timed out and was killed"}
    for line in proc.stdout.splitlines():
        if line.startswith(PASS_MARKER):
            try:
                return json.loads(line[len(PASS_MARKER):])
            except json.JSONDecodeError:
                break
    return {"error": f"no result from the {platform} pass (exit {proc.returncode}); "
                      "most likely no matching Qt platform plugin, or a Qt crash"}


def compute_verdict(wayland: dict, in_input_group: bool, uinput_writable: bool) -> tuple[str, list[str]]:
    if not uinput_writable:
        return "NO-GO", ["/dev/uinput is not writable; nothing could be injected. Run --setup."]
    if wayland.get("error"):
        return "INCONCLUSIVE", [f"wayland pass produced no result: {wayland['error']}"]
    if wayland.get("click_timeout"):
        return "INCONCLUSIVE", ["nobody clicked inside the oracle window within "
                                 f"{CLICK_TIMEOUT_S}s."]

    platform_name = wayland.get("platform_name")
    if platform_name != "wayland":
        return "INCONCLUSIVE", [
            f"forced QT_QPA_PLATFORM=wayland did not stick (platformName()={platform_name!r}); "
            "nothing ran natively. Check the available Qt platform plugins printed above - "
            "the Qt Wayland platform plugin package is probably not installed."]

    gates = wayland.get("gates", {})
    a1 = gates.get("A1", {}).get("pass")
    r1 = gates.get("R1", {}).get("pass")
    if not a1:
        return "NO-GO", ["A1 (acquisition handshake) failed: libinput/KWin never picked up "
                          "the virtual pointer device."]
    if not r1:
        return "NO-GO", ["R1 (key/click/wheel delivery) failed."]

    r2a = gates.get("R2a", {})
    r2b = gates.get("R2b", {})
    r2_pass = bool(r2a.get("pass")) or bool(r2b.get("pass"))
    r2_not_observed = r2a.get("status") == "not_observed" and r2b.get("status") == "not_observed"
    screens = wayland.get("screens", [])
    c1_report = wayland.get("c1", {})
    c1 = c1_report.get("pass")
    # The child re-derives its own census in its own process; prefer it over the parent's,
    # since they run at different times and only the child's copy is what C1 actually saw.
    if "in_input_group" in c1_report:
        in_input_group = c1_report["in_input_group"]

    reasons: list[str] = []

    if not r2_pass and r2_not_observed and len(screens) > 1:
        return "INCONCLUSIVE", [
            "R2a/R2b were not observed and this session reports more than one output; "
            "absolute positioning may have landed on a screen the oracle isn't on. "
            "Re-run with the oracle window moved to the other output before concluding "
            "Partial GO."]

    if r2_pass:
        if c1 is True:
            return "GO", []
        if c1 is False and in_input_group:
            return "NO-GO for capture", [
                "A1/R1/R2 all passed but C1 (evdev capture) failed while in the input group - "
                "a real capability gap, not a setup problem."]
        return "INCONCLUSIVE", ["C1 did not pass and the input group grant is not confirmed "
                                 "active - an unfinished re-login, not a finding."]

    if len(screens) > 1:
        return "NO-GO", ["Absolute positioning failed on a multi-output setup with no clean "
                          "re-run signal (R2 was observed but missed, not merely absent)."]

    if c1 is True:
        return "Partial GO", ["Relative motion (A1, R1, C1) works; absolute positioning "
                               "(R2a/R2b) does not - move-by-relative-delta is the degraded "
                               "capability available on this compositor."]
    if c1 is False and in_input_group:
        return "NO-GO for capture", [
            "A1/R1 passed, R2 did not, and C1 (evdev capture) failed while in the input "
            "group - a real capability gap, not a setup problem."]
    return "INCONCLUSIVE", ["C1 did not pass and the input group grant is not confirmed "
                             "active - an unfinished re-login, not a finding."]


def print_report(wayland: dict, xcb: dict, p1: dict, in_input_group: bool,
                  uinput_writable: bool, verdict: str, reasons: list[str]) -> None:
    print("\n" + "=" * 70)
    print("VERDICT - paste this whole block back")
    print("=" * 70)
    print(f"uinput writable: {uinput_writable}   in input group: {in_input_group}")

    for label, report in (("wayland (forced)", wayland), ("xcb (XWayland, informational)", xcb)):
        print(f"\n--- {label} pass ---")
        if report.get("error"):
            print(f"  {report['error']}")
            continue
        print(f"  platformName()={report.get('platform_name')!r}  "
              f"binding={report.get('binding')}  screens={report.get('screens')}")
        if report.get("click_timeout"):
            print("  nobody clicked inside the oracle window")
            continue
        if report.get("aborted"):
            print("  run aborted (oracle window lost focus)")
        gates = report.get("gates", {})
        for gate in ("A1", "R1", "R2a", "R2b", "R2c", "Rrel"):
            if gate in gates:
                print(f"  {gate}: {gates[gate]}")
        print(f"  C1: {report.get('c1')}")

    print("\n--- P1 (grab + self-filter, own device only, informational) ---")
    print(f"  {p1}")

    print(f"\n{'=' * 70}\nVERDICT: {verdict}\n{'=' * 70}")
    for r in reasons:
        print(f"  - {r}")

    summary = {
        "verdict": verdict, "reasons": reasons,
        "uinput_writable": uinput_writable, "in_input_group": in_input_group,
        "wayland": wayland, "xcb": xcb, "p1": p1,
    }
    print("\nJSON summary:")
    print(json.dumps(summary))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", action="store_true",
                         help="print the distro-detected package/privilege setup block and exit")
    parser.add_argument("--child-pass", choices=("wayland", "xcb"),
                         help=argparse.SUPPRESS)  # internal, used by the subprocess re-invoke
    args = parser.parse_args()

    if args.child_pass:
        run_pass(args.child_pass)
        return

    if args.setup:
        print_setup_block()
        return

    print("macrorec Wayland/KWin go/no-go spike")
    report_env()

    uinput_writable = os.access("/dev/uinput", os.W_OK)
    keyboard_node, in_input_group = find_keyboard_node()
    print(f"\n/dev/uinput writable: {uinput_writable}")
    print(f"keyboard node: {keyboard_node}  in input group: {in_input_group}")
    if not uinput_writable or not in_input_group:
        print("Missing privileges - run `python3 wayland_kwin_spike.py --setup` for the "
              "copy-paste block, log out and back in, then re-run.")

    available_plugins = enumerate_qt_platform_plugins()
    print(f"\nAvailable Qt platform plugins: {available_plugins}")
    if not any("wayland" in p for p in available_plugins):
        print("No Qt Wayland platform plugin found - the wayland pass below will very likely "
              "be INCONCLUSIVE. See --setup for the package to install (e.g. qtwayland5).")

    if uinput_writable:
        print("\nA window will appear. Click inside it, then follow its on-screen text.")
        wayland = run_child_pass("wayland")
        xcb = run_child_pass("xcb")
    else:
        skip = {"error": "skipped: /dev/uinput is not writable"}
        wayland, xcb = dict(skip), dict(skip)

    p1 = run_p1(uinput_writable, in_input_group)

    verdict, reasons = compute_verdict(wayland, in_input_group, uinput_writable)
    print_report(wayland, xcb, p1, in_input_group, uinput_writable, verdict, reasons)


if __name__ == "__main__":
    main()
