# Maintenance prompts — the living document (techne#219)

techne maintains your knowledge with three LLM prompts. They ship with baked-in
defaults but are **operator-editable**: this is the "how techne maintains my
knowledge" document you co-evolve over time, per coherence-layer.md D5.

| name          | drives                          | rendered in            |
|---------------|---------------------------------|------------------------|
| `reflect`     | session → durable candidates    | `core/reflect.py`      |
| `consolidate` | dedup/merge overlapping entries | `core/sleepdream.py`   |
| `dream`       | recombine entries → new insight | `core/sleepdream.py`   |

## How to edit

```
techne prompts export     # writes the current defaults to <data_dir>/prompts/*.md
techne prompts path       # prints that directory
```

Edit `reflect.md` / `consolidate.md` / `dream.md`. The change is picked up on the
**next** reflect/consolidate/dream run — **no code edit, no restart** (the file is
read fresh each render). Delete a file to fall back to the built-in default.

## The contract

Each prompt is a Python `str.format()` template. Your override **must** keep the
`{placeholder}` fields it's given, and any literal `{` / `}` (e.g. in the JSON
schema you ask the model to return) must be **doubled** (`{{` / `}}`).

| prompt        | required placeholders |
|---------------|-----------------------|
| `reflect`     | `{label}` `{summary}` `{observations}` `{existing}` `{knowledge_types}` `{max_items}` |
| `consolidate` | `{entries}` `{types}` |
| `dream`       | `{entries}` `{types}` |

**Safety:** if an override fails to render — a missing/typo'd placeholder, an
un-doubled brace — techne logs a warning and **falls back to the built-in
default** for that run. A fat-fingered prompt can never break the maintenance
loop. (Formatting the built-in default is assumed correct; if *that* ever raises,
it's a code bug, not operator error.)

## Identity layer

The identity layer (`user.*` / persona facts) is the other half of "how techne
maintains my knowledge," and it is **already** operator-editable — it is stored
data, set via `techne identity` and refined by the reflect loop, not a hardcoded
prompt. No override file is needed; edit it through its normal surface.
