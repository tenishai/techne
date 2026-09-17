"""techne#947 — a persisted absolute db_path silently overrode TECHNE_HOME.

`techne init` saved the resolved default `db_path`/`data_dir` as absolute paths.
Copy the home directory, export TECHNE_HOME at the copy, and every write still
went to the original store, with nothing said. Found by Grip while following a
"work on a copy" instruction.
"""

from __future__ import annotations

import yaml

from techne.config import Config


def test_saving_a_default_config_does_not_pin_the_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("TECHNE_HOME", str(home))
    Config.load().save()

    saved = yaml.safe_load((home / "config.yaml").read_text())
    assert "db_path" not in saved, "a default db_path written absolutely pins this home"
    assert "data_dir" not in saved


def test_a_copied_home_uses_the_copy(monkeypatch, tmp_path):
    """The reported scenario: init a home, copy it, point TECHNE_HOME at the copy."""
    import shutil

    original = tmp_path / "original"
    copy = tmp_path / "copy"
    monkeypatch.setenv("TECHNE_HOME", str(original))
    Config.load().save()
    shutil.copytree(original, copy)

    monkeypatch.setenv("TECHNE_HOME", str(copy))
    cfg = Config.load()
    assert cfg.db_path == copy / "techne.db"
    assert cfg.data_dir == copy


def test_an_explicit_path_elsewhere_is_still_saved_and_honoured(monkeypatch, tmp_path):
    home = tmp_path / "home"
    elsewhere = tmp_path / "volume" / "mem.db"
    monkeypatch.setenv("TECHNE_HOME", str(home))
    cfg = Config.load()
    cfg.db_path = elsewhere
    cfg.save()

    assert yaml.safe_load((home / "config.yaml").read_text())["db_path"] == str(elsewhere)
    assert Config.load().db_path == elsewhere


def test_a_persisted_path_outside_techne_home_warns_on_stderr(monkeypatch, tmp_path, capsys):
    """Existing config files already carry absolute paths, so saving less is not
    enough: loading one that points outside TECHNE_HOME must say so."""
    home = tmp_path / "copy"
    home.mkdir()
    stale = tmp_path / "original" / "techne.db"
    (home / "config.yaml").write_text(yaml.dump({"db_path": str(stale)}))
    monkeypatch.setenv("TECHNE_HOME", str(home))

    cfg = Config.load()

    captured = capsys.readouterr()
    assert cfg.db_path == stale, "the persisted path is still honoured, not re-rooted"
    assert "outside TECHNE_HOME" in captured.err and str(stale) in captured.err
    assert captured.out == "", "hooks and the MCP server own stdout"


def test_the_warning_is_not_repeated_in_one_process(monkeypatch, tmp_path, capsys):
    home = tmp_path / "copy2"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.dump({"data_dir": str(tmp_path / "elsewhere")}))
    monkeypatch.setenv("TECHNE_HOME", str(home))

    Config.load()
    Config.load()
    assert capsys.readouterr().err.count("outside TECHNE_HOME") == 1


def test_a_path_inside_techne_home_does_not_warn(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home3"
    home.mkdir()
    (home / "config.yaml").write_text(yaml.dump({"db_path": str(home / "custom" / "m.db")}))
    monkeypatch.setenv("TECHNE_HOME", str(home))

    Config.load()
    assert "outside TECHNE_HOME" not in capsys.readouterr().err


def test_no_warning_without_techne_home(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("TECHNE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # keep the default home off the real store
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.dump({"db_path": str(tmp_path / "somewhere" / "m.db")}))

    Config.load(cfg_file)
    assert "outside TECHNE_HOME" not in capsys.readouterr().err
