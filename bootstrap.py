#!/usr/bin/env python3
"""Sets up the venv, installs dependencies, and starts macrorec, with progress
feedback visible even when there is no attached terminal (a double-clicked
LAUNCH_macrorec_LinuxMac.sh).

Invoked by LAUNCH_macrorec_LinuxMac.sh, which only locates a Python 3
interpreter before handing off here. Stdlib only: this runs before python-xlib
and PyQt5 are guaranteed to be installed, so it must not import anything from
macrorec/.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
VENV_SENTINEL = VENV_DIR / ".bootstrap_complete"
REQUIREMENTS = ROOT / "requirements.txt"
READY_TIMEOUT = 30.0

_TERMINAL_CANDIDATES = [
    ("x-terminal-emulator", ["-e"]),
    ("gnome-terminal", ["--"]),
    ("konsole", ["-e"]),
    ("xterm", ["-e"]),
]


def venv_python_path(venv_dir: Path, os_name: str = os.name) -> Path:
    return (venv_dir / "Scripts" / "python.exe" if os_name == "nt"
            else venv_dir / "bin" / "python3")


VENV_PYTHON = venv_python_path(VENV_DIR)


def display_problem(env=None) -> tuple[str, str] | None:
    """Check the session can run macrorec at all, before a venv is built.

    Returns (severity, message) where severity is "fatal" or "warning", or None
    if the session looks fine. X11 is not an implementation detail here: with no
    X display there is nothing to record from or inject into, and the Xlib error
    that surfaces otherwise says nothing useful about why.
    """
    env = os.environ if env is None else env
    if sys.platform == "darwin":
        return None  # XQuartz sets DISPLAY only once it is actually running

    if not env.get("DISPLAY"):
        if env.get("WAYLAND_DISPLAY"):
            return ("fatal",
                    "This is a Wayland session with no X server available.\n"
                    "macrorec needs X11: Wayland gives no way for an ordinary "
                    "program to watch or inject input globally.\n"
                    "Log in with an X11 (Xorg) session and try again.")
        return ("fatal",
                "No X display found (DISPLAY is not set).\n"
                "macrorec records and replays X11 input, so it needs a "
                "graphical X session.")

    if env.get("WAYLAND_DISPLAY") or env.get("XDG_SESSION_TYPE") == "wayland":
        return ("warning",
                "This looks like a Wayland session running XWayland.\n"
                "macrorec will start, but it can only see and drive X11 "
                "clients; native Wayland windows are invisible to it.")
    return None


class ConsoleReporter:
    def __init__(self, log_path: Path | None = None) -> None:
        self.log_path = log_path
        if log_path is not None:
            log_path.write_text("", encoding="utf-8")

    def set_status(self, text: str) -> None:
        print(text)
        if self.log_path is not None:
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(text + "\n")

    def pump(self) -> None:
        pass

    def close(self) -> None:
        pass


class Splash:
    def __init__(self) -> None:
        import tkinter as tk

        self.root = tk.Tk()
        self.root.title("macrorec")
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", lambda: None)
        # Sized for the longest real status string at two wrapped lines. A label
        # with no wraplength clips silently rather than growing, since
        # resizable() is off.
        width, height = 360, 130
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - width) // 2
        y = (self.root.winfo_screenheight() - height) // 2
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        tk.Label(self.root, text="macrorec", font=("", 12, "bold")).pack(pady=(16, 4))
        self.status_var = tk.StringVar(value="Starting...")
        tk.Label(self.root, textvariable=self.status_var,
                 wraplength=width - 30, justify="center").pack(padx=15)

    def set_status(self, text: str) -> None:
        self.status_var.set(text)
        self.pump()

    def pump(self) -> None:
        self.root.update()

    def close(self) -> None:
        self.root.destroy()


def relaunch_in_terminal() -> bool:
    """Best effort: re-run inside a real terminal so console output is not lost
    on a double-click launch with neither tkinter nor an attached tty. The env
    flag stops a relaunched copy from trying again."""
    if os.name == "nt" or os.environ.get("MACROREC_BOOTSTRAP_RELAUNCHED"):
        return False
    env = dict(os.environ, MACROREC_BOOTSTRAP_RELAUNCHED="1")
    args = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]]
    if sys.platform == "darwin":
        command = " ".join(shlex.quote(a) for a in args)
        script = f'tell application "Terminal" to do script "{command}"'
        try:
            subprocess.Popen(["osascript", "-e", script])
            return True
        except OSError:
            return False
    for executable, flags in _TERMINAL_CANDIDATES:
        if shutil.which(executable) is None:
            continue
        try:
            subprocess.Popen([executable, *flags, *args], env=env)
            return True
        except OSError:
            continue
    return False


def make_reporter(*, stdout=None):
    stdout = stdout if stdout is not None else sys.stdout
    try:
        return Splash()
    except Exception:
        pass
    if stdout.isatty():
        return ConsoleReporter()
    if relaunch_in_terminal():
        sys.exit(0)
    return ConsoleReporter(log_path=ROOT / "bootstrap_log.txt")


def fail(reporter, message: str) -> None:
    reporter.close()
    print()
    print(f"Something went wrong: {message}")
    log_path = getattr(reporter, "log_path", None)
    if log_path is not None:
        print(f"(a log was also written to {log_path})")
    input("Press Enter to close this window...")
    sys.exit(1)


def run_step(reporter, cmd: list[str], status_text: str) -> int:
    reporter.set_status(status_text)
    process = subprocess.Popen(cmd)
    while process.poll() is None:
        reporter.pump()
        time.sleep(0.05)
    return process.returncode


def find_or_create_venv(reporter) -> None:
    if VENV_PYTHON.exists() and VENV_SENTINEL.exists():
        return
    if VENV_DIR.exists():
        # The interpreter appears early in `python -m venv`, well before the
        # venv is usable, so its presence without the sentinel means an
        # interrupted run. Trusting a half-built venv produces confusing pip
        # errors instead of fixing itself.
        shutil.rmtree(VENV_DIR)
    # --system-site-packages so a distribution-packaged PyQt5 is visible and
    # nothing is downloaded for it.
    created = run_step(
        reporter,
        [sys.executable, "-m", "venv", "--system-site-packages", str(VENV_DIR)],
        "First-time setup, this can take a minute...")
    if created != 0:
        fail(reporter, "couldn't create the Python virtual environment (.venv).")
    if run_step(reporter,
                [str(VENV_PYTHON), "-m", "pip", "install", "--quiet",
                 "--upgrade", "pip"],
                "Upgrading pip...") != 0:
        fail(reporter, "couldn't update pip.")
    VENV_SENTINEL.touch()


def install_dependencies(reporter) -> None:
    cmd = [str(VENV_PYTHON), "-m", "pip", "install", "--quiet",
           "-r", str(REQUIREMENTS)]
    status = ("Checking dependencies (only downloads anything the first time, "
              "or after an update)...")
    if run_step(reporter, cmd, status) != 0:
        fail(reporter,
             "couldn't install dependencies. Check your internet connection.\n"
             "If PyQt5 is the problem, installing your distribution's package "
             "(python3-pyqt5 on Debian and Ubuntu) usually fixes it.")


def wait_until_ready(reporter, ready_file: Path, process, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready_file.exists() or process.poll() is not None:
            return
        reporter.pump()
        time.sleep(0.05)


def launch_app(reporter) -> None:
    reporter.set_status("Starting macrorec...")
    ready_file = (Path(tempfile.gettempdir())
                  / f"macrorec_ready_{os.getpid()}_{int(time.time() * 1000)}")
    env = dict(os.environ, MACROREC_READY_FILE=str(ready_file))
    process = subprocess.Popen(
        [str(VENV_PYTHON), "-m", "macrorec.gui", *sys.argv[1:]],
        cwd=str(ROOT), env=env)
    wait_until_ready(reporter, ready_file, process, READY_TIMEOUT)
    reporter.close()
    ready_file.unlink(missing_ok=True)
    code = process.wait()
    if code != 0:
        print(f"macrorec closed with an error (exit code {code}).")
    sys.exit(code)


def main() -> None:
    reporter = make_reporter()
    try:
        problem = display_problem()
        if problem is not None:
            severity, message = problem
            if severity == "fatal":
                fail(reporter, message)
            reporter.set_status(message)
            time.sleep(2.0)
        find_or_create_venv(reporter)
        install_dependencies(reporter)
        launch_app(reporter)
    except SystemExit:
        raise
    except Exception as exc:
        # Last resort: an uncaught exception here would otherwise just vanish
        # the window on a double-click launch with no console attached.
        fail(reporter, str(exc))


if __name__ == "__main__":
    main()
