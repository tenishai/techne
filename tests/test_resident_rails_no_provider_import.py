"""AST guard: the resident emit/ingest rails import no provider SDK DIRECTLY (#499).

techne's resident-model rails — reflect, sleepdream (consolidate/dream),
faithfulness, memory_claims — have a RESIDENT path that runs on the caller's own
model via emit/ingest and needs no API key. That is the invariant this test
guards, and it is narrower than "these modules never touch a provider SDK":
nothing stops a future convenience import — one `import litellm` written directly
inside a rail function to make one thing work — from silently moving the
*default* resident path onto a provider key, with every other test staying
green. This test catches exactly that: a provider SDK named in the rail
module's OWN source, module-level or deferred.

Walks the WHOLE AST rather than grepping, so a DEFERRED import inside a function
body is caught exactly like a module-level one — a lesson #489 (fastconfig's sibling
guard) already paid for: a relative import deferred inside a function evaded three
`sys.modules`-based checks while pulling in the exact thing they existed to catch.

`thread_orchestrator.py` is the deliberate, named exception (techne#19's operator-model
path for autonomous/headless runs, gated behind the `[llm]` extra) — it is not one of
the four rail modules and is not scanned here.

NOT GUARDED, AND DELIBERATELY SO: reflect.py already imports `_make_generate_fn`
from the `techne.core.threads` shim package, which re-exports it from
`thread_orchestrator.py` — so reflect.py transitively reaches the module that
imports litellm. That composition is the documented operator-model/headless route
(reflect-pipeline.md), not a hole this test misses: it is a SEPARATE code path
from the resident emit/ingest one, selected only when `config.model` names a real
API model. Catching that at the AST level would mean forbidding the import
entirely, which would break the legitimate headless path. What this test can and
does prevent is the *rail module itself* silently starting to `import litellm` —
the case #499 asked for.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: The four resident rail modules this invariant covers. thread_orchestrator.py is
#: the named exemption from #499 and is deliberately absent from this tuple.
RAIL_MODULES = (
    "techne/core/reflect.py",
    "techne/core/sleepdream.py",
    "techne/core/faithfulness.py",
    "techne/core/memory_claims.py",
)

#: Provider SDKs (and the transports they're built on) a resident rail must never
#: import, module-level or deferred. Matched on the top-level package name, so
#: `import litellm.utils` and `from anthropic import Anthropic` both trip it.
PROVIDER_MODULES = frozenset({"litellm", "anthropic", "openai", "httpx", "requests", "aiohttp"})


def _provider_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [
                a.name for a in node.names if a.name.split(".")[0] in PROVIDER_MODULES
            ]
        elif isinstance(node, ast.ImportFrom):
            # A relative import (`from . import x`) carries no provider module name
            # and cannot match this list by construction; nothing in PROVIDER_MODULES
            # is a package these rails would import from relatively.
            if node.module and node.module.split(".")[0] in PROVIDER_MODULES:
                offenders.append(node.module)
    return offenders


def test_resident_rails_import_no_provider_sdk() -> None:
    all_offenders: dict[str, list[str]] = {}
    for rel in RAIL_MODULES:
        found = _provider_imports(REPO / rel)
        if found:
            all_offenders[rel] = found
    assert not all_offenders, (
        f"resident rail(s) import a provider SDK, directly or deferred: {all_offenders}. "
        "These rails run on the caller's own model via emit/ingest and must need no "
        "API key — route a genuinely operator-model need through "
        "core/thread_orchestrator.py instead, which is the one named exemption."
    )


def test_the_rail_modules_still_exist() -> None:
    """Guards the guard: a renamed/removed file would make the scan above vacuous
    (it would simply find nothing to import from) rather than fail loudly."""
    missing = [rel for rel in RAIL_MODULES if not (REPO / rel).is_file()]
    assert missing == [], f"rail module(s) no longer exist at the recorded path: {missing}"
