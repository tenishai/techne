"""Contract assertions for techne's public API.

Cabal and limenal depend on specific names and call signatures. A rename
or removal that breaks them shows up here immediately rather than as a
mysterious import error in a sibling repo's CI.

These tests run in the unit CI (no private deps needed) and are the
primary reason techne's CI matters most: both siblings install techne
as an editable sibling and wrap every call in except Exception, so a
breaking change in techne appears as silently missing memory, not an
error.
"""
import inspect


def test_init_db_callable() -> None:
    import techne
    assert callable(techne.init_db), "techne.init_db must be callable"
    sig = inspect.signature(techne.init_db)
    params = list(sig.parameters)
    assert params[0] == "db_path", (
        f"techne.init_db first param must be 'db_path', got {params[0]!r}"
    )


def test_search_search_callable() -> None:
    import techne
    assert hasattr(techne, "search"), "techne.search submodule must exist"
    assert callable(techne.search.search), "techne.search.search must be callable"
    sig = inspect.signature(techne.search.search)
    params = list(sig.parameters)
    assert params[0] == "db_path", (
        f"techne.search.search first param must be 'db_path', got {params[0]!r}"
    )
    assert "query" in params, "techne.search.search must accept 'query' param"


def test_knowledge_store_callable() -> None:
    import techne
    assert hasattr(techne, "knowledge"), "techne.knowledge submodule must exist"
    assert callable(techne.knowledge.store), "techne.knowledge.store must be callable"
    sig = inspect.signature(techne.knowledge.store)
    params = list(sig.parameters)
    assert params[0] == "db_path", (
        f"techne.knowledge.store first param must be 'db_path', got {params[0]!r}"
    )
    assert "content" in params, "techne.knowledge.store must accept 'content' param"


def test_lifecycle_functions_present() -> None:
    """lifecycle is used by cabal's session tracker but not in __all__."""
    import techne.lifecycle as lifecycle
    for name in ("run_session_start", "run_session_end", "capture_tool_use"):
        assert callable(getattr(lifecycle, name, None)), (
            f"techne.lifecycle.{name} must be callable"
        )


def test_public_api_all_importable() -> None:
    """Every name in __all__ must actually be importable from techne."""
    import techne
    for name in techne.__all__:
        obj = getattr(techne, name, None)
        assert obj is not None, f"techne.__all__ lists {name!r} but it is None/absent"


def test_knowledge_type_field_present() -> None:
    """Limenal memory handler reads r.get('knowledge_type') from search results.

    This field name must not change without coordinating with limenal.
    """
    from pathlib import Path
    import tempfile
    import techne

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "test.db"
        techne.init_db(db)
        techne.knowledge.store(db, content="hello world", type="fact", source="test")
        results = techne.search.search(db, "hello", table="knowledge", limit=1)
        assert results, "search should return at least one result"
        r = results[0]
        assert "knowledge_type" in r or "type" in r, (
            "search result must have 'knowledge_type' or 'type' field "
            "(limenal reads r.get('knowledge_type') or r.get('type'))"
        )
