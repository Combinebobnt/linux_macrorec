"""Persisted preferences, as JSON under `$XDG_CONFIG_HOME/macrorec/`.

Unknown keys in the file are kept on save. A future version's settings should not be
destroyed by an older one that happens to open the file first.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields

DEFAULTS = {
    # Global hotkeys: server-side grabs, so they work with any window focused.
    "panic_key": "Escape",
    # Unbound by default. A global grab on a bare key would otherwise take that
    # key away from every other program the moment macrorec is opened.
    "record_key": "",
    "play_key": "",
    # Window shortcuts: ordinary Qt keybindings, live only while macrorec has
    # focus, so they can safely default to the conventional combinations.
    "open_key": "Ctrl+O",
    "save_key": "Ctrl+S",
    "save_as_key": "Ctrl+Shift+S",
    "reload_key": "Ctrl+R",
    "always_on_top": True,
    "speed": 1.0,
    "loops": 1,
    "last_directory": "",
}


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "macrorec")


def config_path() -> str:
    return os.path.join(config_dir(), "settings.json")


@dataclass
class Settings:
    panic_key: str = DEFAULTS["panic_key"]
    record_key: str = DEFAULTS["record_key"]
    play_key: str = DEFAULTS["play_key"]
    open_key: str = DEFAULTS["open_key"]
    save_key: str = DEFAULTS["save_key"]
    save_as_key: str = DEFAULTS["save_as_key"]
    reload_key: str = DEFAULTS["reload_key"]
    always_on_top: bool = DEFAULTS["always_on_top"]
    speed: float = DEFAULTS["speed"]
    loops: int = DEFAULTS["loops"]
    last_directory: str = DEFAULTS["last_directory"]
    #: Anything this version does not know about, carried through on save.
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | None = None) -> "Settings":
        path = path or config_path()
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()

        known = {f.name for f in fields(cls)} - {"extra"}
        settings = cls()
        for name in known:
            if name in data:
                setattr(settings, name, _coerce(getattr(settings, name), data[name]))
        settings.extra = {k: v for k, v in data.items() if k not in known}
        return settings

    def save(self, path: str | None = None) -> str:
        path = path or config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = asdict(self)
        data.update(data.pop("extra"))
        # Write via a temporary file so an interrupted save cannot truncate the old
        # settings to nothing.
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
        return path


def _coerce(current, value):
    """Keep the default's type. A hand-edited file should not turn `loops` into a
    string and break playback somewhere far away."""
    try:
        if isinstance(current, bool):
            return bool(value)
        if isinstance(current, int):
            return int(value)
        if isinstance(current, float):
            return float(value)
        if isinstance(current, str):
            return str(value)
    except (TypeError, ValueError):
        pass
    return current
