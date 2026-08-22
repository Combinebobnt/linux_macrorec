"""Tests for the launcher's pure logic and for the launcher/packaging files.

`bootstrap.py` is stdlib-only on purpose (it runs before dependencies exist), so
it imports cleanly here regardless of what is installed.
"""

from __future__ import annotations

import os
import re
import stat
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bootstrap  # noqa: E402

LAUNCHER = ROOT / "LAUNCH_macrorec_LinuxMac.sh"


# --- packaging consistency ---------------------------------------------------


def requirement_names(text: str) -> set[str]:
    names = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        names.add(re.split(r"[<>=!~\[]", line)[0].strip().lower())
    return names


def test_requirements_matches_pyproject():
    """Two files listing dependencies is two files that can disagree."""
    with open(ROOT / "pyproject.toml", "rb") as handle:
        project = tomllib.load(handle)["project"]

    declared = {re.split(r"[<>=!~\[]", d)[0].strip().lower()
                for d in project["dependencies"]}
    for extra in project.get("optional-dependencies", {}).values():
        declared |= {re.split(r"[<>=!~\[]", d)[0].strip().lower() for d in extra}

    assert requirement_names(bootstrap.REQUIREMENTS.read_text()) == declared


def test_requirements_file_is_where_bootstrap_expects():
    assert bootstrap.REQUIREMENTS.exists()
    assert bootstrap.REQUIREMENTS.name == "requirements.txt"


# --- the launcher script -----------------------------------------------------


def test_launcher_exists_and_is_executable():
    assert LAUNCHER.exists(), "the double-click launcher is missing"
    mode = LAUNCHER.stat().st_mode
    assert mode & stat.S_IXUSR, (
        f"{LAUNCHER.name} is not executable, so it cannot be double-clicked "
        f"and git would record it as 100644.\n"
        f"Fix with:  chmod +x {LAUNCHER.name}")


def test_launcher_hands_off_to_bootstrap():
    text = LAUNCHER.read_text()
    assert text.startswith("#!/usr/bin/env bash")
    assert "bootstrap.py" in text
    assert 'cd "$(dirname "${BASH_SOURCE[0]}")"' in text, (
        "must run from the repo root however it was invoked")
    assert '"$@"' in text, "a macro path argument must reach the app"


# --- venv layout -------------------------------------------------------------


def test_venv_python_path_per_platform():
    assert bootstrap.venv_python_path(Path("/x/.venv"), "posix") == Path(
        "/x/.venv/bin/python3")
    assert bootstrap.venv_python_path(Path("/x/.venv"), "nt") == Path(
        "/x/.venv/Scripts/python.exe")


def test_the_venv_is_built_with_system_site_packages():
    """A distribution-packaged PyQt5 is only visible through that flag, and
    without it every launch downloads Qt from PyPI instead."""
    source = (ROOT / "bootstrap.py").read_text()
    assert "--system-site-packages" in source


def test_the_sentinel_lives_inside_the_venv():
    """So wiping .venv also clears the 'setup finished' marker."""
    assert bootstrap.VENV_SENTINEL.parent == bootstrap.VENV_DIR


# --- session checks ----------------------------------------------------------


def test_a_plain_x11_session_is_fine():
    assert bootstrap.display_problem(
        {"DISPLAY": ":0", "XDG_SESSION_TYPE": "x11"}) is None


def test_wayland_with_no_x_server_is_fatal():
    severity, message = bootstrap.display_problem(
        {"WAYLAND_DISPLAY": "wayland-0", "XDG_SESSION_TYPE": "wayland"})
    assert severity == "fatal"
    assert "Wayland" in message and "X11" in message


def test_no_display_at_all_is_fatal():
    severity, message = bootstrap.display_problem({})
    assert severity == "fatal"
    assert "DISPLAY" in message


def test_xwayland_warns_but_does_not_block():
    severity, message = bootstrap.display_problem(
        {"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0",
         "XDG_SESSION_TYPE": "wayland"})
    assert severity == "warning"
    assert "XWayland" in message


def test_macos_is_not_second_guessed(monkeypatch):
    """XQuartz sets DISPLAY only once it is running, so an empty environment
    there is not evidence of anything."""
    monkeypatch.setattr(bootstrap.sys, "platform", "darwin")
    assert bootstrap.display_problem({}) is None


# --- the ready handshake -----------------------------------------------------


def test_the_app_signals_ready_for_the_splash(tmp_path, monkeypatch):
    from macrorec import gui

    marker = tmp_path / "ready"
    monkeypatch.setenv("MACROREC_READY_FILE", str(marker))
    gui._signal_ready()
    assert marker.exists(), "bootstrap's splash would never close"


def test_signalling_ready_is_a_no_op_when_not_launched_by_bootstrap(monkeypatch):
    from macrorec import gui

    monkeypatch.delenv("MACROREC_READY_FILE", raising=False)
    gui._signal_ready()  # must not raise


def test_an_unwritable_ready_path_does_not_kill_the_app(monkeypatch):
    from macrorec import gui

    monkeypatch.setenv("MACROREC_READY_FILE", "/nonexistent-dir/ready")
    gui._signal_ready()  # the app must start regardless


def test_bootstrap_passes_the_ready_file_to_the_app():
    source = (ROOT / "bootstrap.py").read_text()
    assert "MACROREC_READY_FILE" in source
    assert "-m" in source and "macrorec.gui" in source


@pytest.mark.parametrize("module", ["os", "shutil", "subprocess", "tempfile"])
def test_bootstrap_uses_only_the_standard_library(module):
    """It runs before dependencies are installed, so importing anything from
    macrorec/ or a third-party package would break the first launch."""
    source = (ROOT / "bootstrap.py").read_text()
    assert f"import {module}" in source
    assert not re.search(r"^\s*(from|import)\s+macrorec", source, re.MULTILINE)
    assert not re.search(r"^\s*(from|import)\s+(PyQt5|Xlib)", source, re.MULTILINE)


def test_bootstrap_is_importable_without_the_projects_dependencies():
    assert callable(bootstrap.main)
    assert os.path.exists(bootstrap.__file__)
