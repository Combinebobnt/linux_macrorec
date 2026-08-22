import json
import os

from macrorec.settings import Settings, config_dir, config_path


def test_defaults_when_no_file_exists(tmp_path):
    settings = Settings.load(str(tmp_path / "absent.json"))
    assert settings.panic_key == "Escape"
    assert settings.always_on_top is True
    assert settings.speed == 1.0
    assert settings.loops == 1


def test_save_then_load_round_trip(tmp_path):
    path = str(tmp_path / "settings.json")
    original = Settings(panic_key="F12", always_on_top=False, speed=2.5, loops=0,
                        last_directory="/tmp/macros")
    original.save(path)
    assert Settings.load(path) == original


def test_save_creates_the_directory(tmp_path):
    path = str(tmp_path / "nested" / "deeper" / "settings.json")
    Settings().save(path)
    assert os.path.exists(path)


def test_unknown_keys_survive_a_load_save_cycle(tmp_path):
    """An older build must not wipe a newer one's settings."""
    path = str(tmp_path / "settings.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"panic_key": "F12", "future_option": [1, 2, 3]}, handle)

    settings = Settings.load(path)
    assert settings.panic_key == "F12"
    settings.save(path)

    with open(path, encoding="utf-8") as handle:
        written = json.load(handle)
    assert written["future_option"] == [1, 2, 3]
    assert written["panic_key"] == "F12"


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    path = str(tmp_path / "settings.json")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{not json at all")
    assert Settings.load(path) == Settings()


def test_a_json_list_is_not_a_settings_file(tmp_path):
    path = str(tmp_path / "settings.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump([1, 2, 3], handle)
    assert Settings.load(path) == Settings()


def test_wrong_types_are_coerced_not_propagated(tmp_path):
    path = str(tmp_path / "settings.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"loops": "3", "speed": "0.5", "always_on_top": 0}, handle)

    settings = Settings.load(path)
    assert settings.loops == 3 and isinstance(settings.loops, int)
    assert settings.speed == 0.5
    assert settings.always_on_top is False


def test_uncoercible_value_keeps_the_default(tmp_path):
    path = str(tmp_path / "settings.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"loops": "not a number"}, handle)
    assert Settings.load(path).loops == 1


def test_config_path_follows_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config_dir() == str(tmp_path / "macrorec")
    assert config_path() == str(tmp_path / "macrorec" / "settings.json")


def test_config_path_falls_back_to_dot_config(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert config_dir().endswith("/.config/macrorec")
