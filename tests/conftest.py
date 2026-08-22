"""Test fixtures, including the headless X server the backend tests run against.

Three environment constraints are baked in here deliberately. See `AGENTS.md`:

- Xvfb must listen on TCP; its unix socket fails on the development machine. The
  `_XSERVTrans... Unable to open socket` lines it prints are expected.
- The server has to be spawned by the process that talks to it. One launched by an
  earlier shell invocation is not reachable.
- Xvfb leaves `/tmp/.X<n>-lock` behind when killed, which blocks reuse of that
  display number, so the lock is removed on teardown.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

import pytest  # noqa: F401  (re-exported implicitly via fixtures)

SCREEN = "1024x768x24"


def _free_display(low: int = 50, high: int = 90) -> int:
    for number in range(low, high):
        if not os.path.exists(f"/tmp/.X{number}-lock"):
            return number
    raise RuntimeError("no free X display number")


class XvfbServer:
    def __init__(self, number: int):
        self.number = number
        self.name = f"127.0.0.1:{number}"
        self.lock = f"/tmp/.X{number}-lock"
        self.process = None

    def start(self) -> "XvfbServer":
        self.process = subprocess.Popen(
            ["Xvfb", f":{self.number}", "-screen", "0", SCREEN,
             "-listen", "tcp", "-ac"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 15
        while time.time() < deadline:
            if self._responds():
                return self
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"Xvfb exited with {self.process.returncode}")
            time.sleep(0.1)
        self.stop()
        raise RuntimeError("Xvfb did not come up")

    def _responds(self) -> bool:
        return subprocess.run(
            ["xdpyinfo", "-display", self.name],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if os.path.exists(self.lock):
            try:
                os.unlink(self.lock)
            except OSError:
                pass


@pytest.fixture(scope="session")
def xvfb():
    if shutil.which("Xvfb") is None:
        pytest.skip("Xvfb is not installed")
    pytest.importorskip("Xlib", reason="python-xlib is not installed")

    server = XvfbServer(_free_display()).start()
    previous = os.environ.get("DISPLAY")
    os.environ["DISPLAY"] = server.name
    try:
        yield server
    finally:
        if previous is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = previous
        server.stop()


@pytest.fixture
def dpy(xvfb):
    """A short-lived connection to the test server."""
    from Xlib import display

    connection = display.Display(xvfb.name)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(scope="session")
def wm_display():
    """A second headless display running a real window manager.

    `marco` is MATE's window manager, and it is the reason the Escape panic stop
    silently failed: it binds Alt+Escape, which makes an `AnyModifier` grab on
    Escape fail wholesale. Running it here means that bug is covered against the
    real thing rather than a stand-in.

    A separate display keeps it away from the other tests, which expect a bare
    server with nothing managing or focusing windows. Session-scoped and lazy, so
    it only starts if a test asks for it.
    """
    if shutil.which("Xvfb") is None or shutil.which("marco") is None:
        pytest.skip("Xvfb and marco are both needed for window-manager tests")
    pytest.importorskip("Xlib", reason="python-xlib is not installed")

    server = XvfbServer(_free_display()).start()
    manager = subprocess.Popen(
        ["marco", "--display", server.name, "--replace"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # marco takes a moment to claim its bindings; the tests are about what it has
    # grabbed, so waiting for it to settle is the point rather than a nicety.
    if not _wait_for_wm(server.name, manager):
        manager.terminate()
        server.stop()
        pytest.skip("marco would not start on the test display")

    try:
        yield server
    finally:
        if manager.poll() is None:
            manager.terminate()
            try:
                manager.wait(5)
            except subprocess.TimeoutExpired:
                manager.kill()
        server.stop()


def _wait_for_wm(display_name: str, process, timeout: float = 15.0) -> bool:
    """True once a window manager owns the screen. Detected by the presence of the
    `_NET_SUPPORTING_WM_CHECK` property, which is how any EWMH-aware program asks
    the same question."""
    from Xlib import Xatom, display

    deadline = time.time() + timeout
    connection = display.Display(display_name)
    try:
        atom = connection.intern_atom("_NET_SUPPORTING_WM_CHECK")
        while time.time() < deadline:
            if process.poll() is not None:
                return False
            prop = connection.screen().root.get_full_property(atom, Xatom.WINDOW)
            if prop and prop.value:
                return True
            time.sleep(0.2)
        return False
    finally:
        connection.close()


@pytest.fixture
def capture(xvfb):
    """A started X11Recorder plus the list it feeds. Stops itself on teardown."""
    from macrorec.backend.x11 import X11Recorder

    recorder = X11Recorder(xvfb.name)
    events = []
    recorder.start(lambda at, event: events.append((at, event)))
    try:
        yield recorder, events
    finally:
        recorder.stop()
