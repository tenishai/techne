"""A BEHAVIOURAL witness for techne#842 on the VEC channel.

The source pin in tests/test_user_prompt_submit_hook.py checks that both
knowledge sites slice by the name `_cap`. That is not enough, and Prudence
demonstrated it on #869 r2: rebinding `_cap = _ROW_CHAR_CAP` inside
`search_vec` — deleting the entire fix on that channel — left **4010 tests
passing** and both source pins green. The pin observes the slice EXPRESSION;
the binding is the fix, and nothing observed the binding.

It matters most on this channel specifically. The vec-first union puts the
vector channel at the head of the page, so on any non-trivial store these are
the rows an agent actually receives.

The reason given for pinning by source rather than behaviour was that driving
the vec channel needs a populated index and a cached model, making the test
slow and environment-dependent. Measured 2026-09-01, that premise was wrong:
one `backfill_vectors()` call and the assertion below run in well under a
second. The cost was never the obstacle — nobody had tried it.

Marked `eval_vec` so it runs in the CI leg that installs `.[dev,codex,vectors]`
with the model pre-cached, rather than skipping in the default legs. A guard
that skips where it is supposed to run witnesses nothing.
"""

import pytest


@pytest.mark.eval_vec
def test_the_vec_channel_honours_row_chars(pinned_model2vec, tmp_path):
    from techne.core.database import init_db
    from techne.core.knowledge import store as store_knowledge
    from techne.core.search import _ROW_CHAR_CAP, backfill_vectors, search_vec

    db = tmp_path / "vec.db"
    init_db(db)
    body = " ".join(f"word{i:03d}" for i in range(300))
    assert len(body) > 2 * _ROW_CHAR_CAP, "fixture must exceed the default cap"
    store_knowledge(db, content=body, type="procedure", tags="probe", confidence="high")
    backfill_vectors(db)

    over = _ROW_CHAR_CAP + 100
    rows = search_vec(db, "word001", table="knowledge", limit=5, row_chars=over)
    assert rows, (
        "the vec channel returned nothing — the index did not populate, so this "
        "test would witness nothing; fix the fixture rather than relaxing this"
    )
    assert len(rows[0]["content"]) == over, (
        f"vec channel returned {len(rows[0]['content'])} chars for row_chars={over}. "
        "If it is exactly 500, `_cap` in search_vec is bound to _ROW_CHAR_CAP rather "
        "than to row_chars — techne#842 is live again on the channel that serves the "
        "head of the page. The source pin cannot see this: it checks the slice reads "
        "`_cap`, not what `_cap` holds."
    )

    default = search_vec(db, "word001", table="knowledge", limit=5)
    assert len(default[0]["content"]) == _ROW_CHAR_CAP, (
        "a vec caller that asks for nothing must still get the documented default"
    )
