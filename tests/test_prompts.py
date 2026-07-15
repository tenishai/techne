"""Operator-editable maintenance prompts (techne#219).

render_prompt prefers a per-install override file, falls back to the baked-in
default (including when a hand-edited override breaks the {placeholder} contract),
and picks up edits live. export_defaults scaffolds the files without clobbering
edits.
"""

from __future__ import annotations

import pytest

from techne.core.prompts import (
    PROMPT_FIELDS,
    default_templates,
    export_defaults,
    prompts_dir,
    render_prompt,
)

_DEFAULT = "DEFAULT reflect for {label}"


@pytest.fixture()
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
    from techne.config import Config

    return Config.load()


def _write_override(config, name, text):
    d = prompts_dir(config)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(text)


def test_uses_default_when_no_override(config):
    assert render_prompt(config, "reflect", _DEFAULT, label="X") == "DEFAULT reflect for X"


def test_override_wins_when_present(config):
    _write_override(config, "reflect", "OVERRIDE for {label}")
    assert render_prompt(config, "reflect", _DEFAULT, label="X") == "OVERRIDE for X"


def test_broken_override_falls_back_to_default(config):
    """A missing/typo'd placeholder in an override must NOT break the run — fall
    back to the built-in default instead."""
    _write_override(config, "reflect", "BROKEN {label} {nonexistent}")
    assert render_prompt(config, "reflect", _DEFAULT, label="X") == "DEFAULT reflect for X"


def test_edit_picked_up_live(config):
    """Editing the override file is reflected on the next render — no restart."""
    _write_override(config, "reflect", "V1 {label}")
    assert render_prompt(config, "reflect", _DEFAULT, label="X") == "V1 X"
    _write_override(config, "reflect", "V2 {label}")
    assert render_prompt(config, "reflect", _DEFAULT, label="X") == "V2 X"


def test_export_writes_defaults_and_skips_existing(config):
    first = export_defaults(config)
    assert set(first) == {"reflect", "consolidate", "dream"}
    assert all(v == "written" for v in first.values())
    for name in first:
        assert (prompts_dir(config) / f"{name}.md").exists()

    # Second export must NOT clobber (an operator may have edited them).
    (prompts_dir(config) / "reflect.md").write_text("MY EDIT")
    second = export_defaults(config)
    assert all(v == "skipped" for v in second.values())
    assert (prompts_dir(config) / "reflect.md").read_text() == "MY EDIT"

    # --overwrite replaces.
    third = export_defaults(config, overwrite=True)
    assert all(v == "written" for v in third.values())
    assert (prompts_dir(config) / "reflect.md").read_text() != "MY EDIT"


@pytest.mark.parametrize("name", ["reflect", "consolidate", "dream"])
def test_default_template_renders_with_documented_fields(name):
    """Contract self-consistency: each baked-in default formats cleanly given
    exactly its PROMPT_FIELDS — catches placeholder drift between the template,
    the documented contract, and the call site."""
    template = default_templates()[name]
    dummy = {field: "x" for field in PROMPT_FIELDS[name]}
    template.format(**dummy)  # raises KeyError if the template needs an undocumented field
