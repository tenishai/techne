"""Operator-editable maintenance prompts — the living-document layer (techne#219).

The reflect / consolidate / dream prompts describe HOW techne maintains knowledge.
They ship with baked-in defaults (co-located with their use in reflect.py /
sleepdream.py) but are overridable per-install: drop a file at
``<data_dir>/prompts/<name>.md`` and the operator co-evolves the prompt WITHOUT a
code edit.

``render_prompt`` loads the override if present — read fresh each call, so an edit
is picked up on the next maintenance run (no restart) — formats it with the runtime
fields, and CRUCIALLY falls back to the baked-in default if a hand-edited override
breaks the ``{placeholder}`` contract. A fat-fingered prompt can never break the
maintenance loop. See ``docs/prompts.md`` for the contract.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# name → the {placeholder} fields each prompt requires. The documented contract
# (docs/prompts.md is generated from the same truth); also lets a test assert the
# baked-in defaults stay consistent with what the call sites actually pass.
PROMPT_FIELDS: dict[str, tuple[str, ...]] = {
    "reflect": ("label", "summary", "observations", "existing", "knowledge_types", "max_items"),
    "consolidate": ("entries", "types"),
    "dream": ("entries", "types"),
}


def prompts_dir(config) -> Path:
    """Directory holding operator prompt overrides: ``<data_dir>/prompts``."""
    return Path(config.data_dir) / "prompts"


def default_templates() -> dict[str, str]:
    """The baked-in default templates, keyed by name. Lazy import keeps this module
    free of a cycle (reflect/sleepdream import core utilities, not vice-versa)."""
    from techne.core.reflect import PROMPT_TEMPLATE
    from techne.core.sleepdream import CONSOLIDATE_PROMPT, DREAM_PROMPT

    return {
        "reflect": PROMPT_TEMPLATE,
        "consolidate": CONSOLIDATE_PROMPT,
        "dream": DREAM_PROMPT,
    }


def _load_override(config, name: str) -> str | None:
    """The operator's override template for *name*, or None. Read fresh each call so
    edits are picked up on the next maintenance run without a restart."""
    try:
        p = prompts_dir(config) / f"{name}.md"
        return p.read_text(encoding="utf-8") if p.exists() else None
    except OSError as e:  # unreadable file / perms — fall back, don't break maintenance
        log.warning("prompts: could not read override %r (%s); using default", name, e)
        return None


def render_prompt(config, name: str, default_template: str, **fields: object) -> str:
    """Render maintenance prompt *name*, preferring an operator override.

    Falls back to *default_template* when there is no override OR the override fails
    to format (a broken ``{placeholder}`` contract — missing/extra/typo'd field).
    Formatting the DEFAULT is assumed correct: if it raises, that's a genuine code
    bug and should surface rather than be swallowed."""
    template = _load_override(config, name)
    if template is not None:
        try:
            return template.format(**fields)
        except Exception as e:  # noqa: BLE001
            # An override is arbitrary operator text — str.format on a broken one
            # can raise KeyError (missing {field}), IndexError ({0}), ValueError
            # (stray brace / bad spec), AttributeError ({field.attr}), TypeError
            # ({field[key]}), and more. The promise is that NO fat-fingered
            # override breaks the maintenance loop, so catch broadly and fall back.
            # A genuine field-passing bug is NOT masked: the default format below
            # runs with the same **fields and will surface it.
            log.warning(
                "prompts: override %r failed to render (%s); using built-in default",
                name,
                e,
            )
    return default_template.format(**fields)


def export_defaults(config, *, overwrite: bool = False) -> dict[str, str]:
    """Write the baked-in defaults to ``<data_dir>/prompts/*.md`` so the operator can
    edit from the real templates. Returns ``{name: "written" | "skipped"}``. Never
    clobbers an already-present (possibly edited) file unless ``overwrite=True``."""
    d = prompts_dir(config)
    d.mkdir(parents=True, exist_ok=True)
    result: dict[str, str] = {}
    for name, template in default_templates().items():
        p = d / f"{name}.md"
        if p.exists() and not overwrite:
            result[name] = "skipped"
            continue
        p.write_text(template, encoding="utf-8")
        result[name] = "written"
    return result
