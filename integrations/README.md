# integrations/

Code a **foreign runtime** loads to talk to techne, as opposed to
`techne/platforms/`, which is techne's own code for adapting to a client.

|                    | `techne/platforms/` | `integrations/` |
|--------------------|----------------------|------------------|
| language           | Python               | TypeScript (today) |
| who runs it        | techne imports and runs it | the foreign client loads and runs it |
| ships in the wheel | yes                  | no |
| how it's installed | `pipx install "techne @ git+https://github.com/tenishai/techne.git@<tag>"` (PyPI `techne` is an unrelated project) | the operator points the client at an absolute path in the source checkout |

If techne imports the code, it belongs in `techne/platforms/`. If a client such
as opencode or pi loads and executes the code itself — even though that code
only shells out to techne's CLI or talks to techne's MCP server — it belongs
here.

Each subdirectory is one client's validation gate for #57 (memory portability
across AI clients): `integrations/opencode/` and `integrations/pi/` each pair
an MCP server registration (model-facing memory: search/recall/store/observe)
with a plugin or extension that shells out to `techne observe` for automatic
capture, because neither client exposes a subprocess hook the way Claude
Code's PostToolUse does.

## Adding an integration

1. Create `integrations/<client>/`.
2. Write its README the same shape as `integrations/opencode/README.md`: how
   to register techne's MCP server, and how the client's own extension
   mechanism should call `techne observe` (or the closest analogue) after a
   tool runs.
3. Nothing here needs a `force-include` in `pyproject.toml` — this directory
   does not ship in the wheel, and the operator's config points at their
   source checkout instead.
4. Do not put the client's code under `techne/platforms/`: techne never
   imports it, so it does not belong in the package techne ships.
