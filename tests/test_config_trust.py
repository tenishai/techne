"""Operator-vs-agent config trust boundary (techne#393).

ON SIMULATING THE LOCKED STATE. A genuinely locked file is one this process
cannot rewrite, which a test running as an unprivileged user cannot create — you
need root to chown. Rather than stub the predicate and call it tested, the
positive case is split:

* :class:`TestLockPredicateAgainstRealFiles` evaluates the REAL predicate against
  a real root-owned file the OS already provides (``/etc/hosts``: uid 0, mode
  0644, root-owned ancestors). Nothing is patched, so every branch — symlink,
  regular-file, uid, mode, ACL, ancestor walk — runs for real.
* The layering tests take the verdict as given and patch ``evaluate_lock``,
  because what they are testing is what ``Config.load`` DOES with a verdict, and
  threading real ownership through them would test the OS twice and the layering
  never.

The distinction matters because a test that patches the thing it claims to check
reports the patch (techne#981).
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest
import yaml

from techne.config import Config
from techne.security import config_trust
from techne.security.config_trust import (
    ABSENT,
    LOCKED,
    UNLOCKED,
    TrustVerdict,
    apply_operator_layer,
    evaluate_lock,
    resolve_operator_config,
    strip_egress,
)

pytestmark = pytest.mark.skipif(
    not hasattr(os, "geteuid"), reason="ownership-based trust requires POSIX uids"
)


def _force(monkeypatch, state: str, path: Path | None = None, reason: str = "test", resolved=None):
    """Pin the trust verdict Config.load will see."""
    verdict = TrustVerdict(state, path, reason, resolved)
    monkeypatch.setattr(config_trust, "evaluate_lock", lambda _p: verdict)
    return verdict


# ---------------------------------------------------------------------------
# The predicate itself, unpatched
# ---------------------------------------------------------------------------


class TestLockPredicateAgainstRealFiles:
    def test_root_owned_file_with_root_owned_ancestors_is_locked(self):
        """The one real positive available without root: /etc/hosts.

        Everything the predicate asserts holds here for real — uid 0 != our euid,
        mode 0644, and /etc and / are both root-owned 0755, so the ancestor walk
        finds nothing writable.
        """
        hosts = Path("/etc/hosts")
        if not hosts.exists():
            pytest.skip("no /etc/hosts on this host")
        if os.geteuid() == 0:
            # A ROOT ENVIRONMENT GETS AN ASSERTION, NOT A SKIP. This is the only
            # unpatched LOCKED coverage in the module, and this repo's own
            # Dockerfile produces a root container — so skipping here left the
            # predicate with zero real coverage while the suite still reported
            # green (Prudence, PR #508). Under root, LOCKED is unreachable BY
            # DESIGN, so the honest test is that the euid-0 branch fires.
            v = evaluate_lock(hosts)
            assert v.state == UNLOCKED and "euid 0" in v.reason
            return
        st = os.stat(hosts)
        if st.st_uid == os.geteuid() or st.st_mode & 0o022:
            pytest.skip("/etc/hosts is not root-owned 0644 on this host")

        verdict = evaluate_lock(hosts)

        assert verdict.state == LOCKED, verdict.reason
        assert str(st.st_uid) in verdict.reason

    def test_absent_path_is_absent_not_unlocked(self):
        """No layer deployed is a different fact from a layer that fails.

        Collapsing them would make every default install look like a broken
        deployment, and the check that reads this would cry wolf on 100% of
        single-user hosts.
        """
        assert evaluate_lock(None).state == ABSENT

    def test_our_own_file_is_unlocked_however_narrow_the_mode(self, tmp_path):
        """0400 and owned by us is not a lock — an owner can chmod it back.

        This is the branch that makes ``~/.techne/config.operator.yaml``
        pointless as a default location, so it is worth pinning rather than
        assuming.
        """
        path = tmp_path / "config.operator.yaml"
        path.write_text("sync: {adapter: noop}\n")
        path.chmod(0o400)

        verdict = evaluate_lock(path)

        assert verdict.state == UNLOCKED
        assert "euid" in verdict.reason

    def test_symlink_is_refused(self, tmp_path):
        """Whoever owns the link chooses the target, so the target's mode is moot."""
        target = tmp_path / "real.yaml"
        target.write_text("sync: {adapter: noop}\n")
        link = tmp_path / "config.operator.yaml"
        link.symlink_to(target)

        verdict = evaluate_lock(link)

        assert verdict.state == UNLOCKED
        assert "symlink" in verdict.reason

    def test_writable_ancestor_defeats_an_otherwise_perfect_file(self, tmp_path, monkeypatch):
        """A file we cannot write, in a directory we can, is replaceable by rename.

        The file's own properties are forced to pass so the ONLY thing that can
        fail is the ancestor walk — otherwise this test would still pass with the
        walk deleted, which is the shape of a test that checks nothing.
        """
        path = tmp_path / "config.operator.yaml"
        path.write_text("sync: {adapter: noop}\n")
        monkeypatch.setattr(os, "geteuid", lambda: os.stat(path).st_uid + 1)
        monkeypatch.setattr(config_trust, "_writable_by_us", lambda p: p != path and p.is_dir())

        verdict = evaluate_lock(path)

        assert verdict.state == UNLOCKED
        assert "parent dir" in verdict.reason
        assert str(tmp_path) in verdict.reason

    def test_directory_is_not_a_config(self, tmp_path):
        d = tmp_path / "config.operator.yaml"
        d.mkdir()
        assert evaluate_lock(d).state == UNLOCKED


class TestResolutionOrder:
    def test_system_path_outranks_the_env_override(self, tmp_path, monkeypatch):
        """The env var must not be able to demote a deployed system layer.

        If it could, an agent would disable the entire mechanism by exporting
        one variable — the resolution order IS the security property here.
        """
        system = tmp_path / "etc" / "config.operator.yaml"
        system.parent.mkdir()
        system.write_text("sync: {adapter: noop}\n")
        elsewhere = tmp_path / "agent-chosen.yaml"
        elsewhere.write_text("sync: {adapter: filesystem}\n")
        monkeypatch.setattr(config_trust, "SYSTEM_OPERATOR_CONFIG", system)
        monkeypatch.setenv(config_trust.OPERATOR_CONFIG_ENV, str(elsewhere))

        assert resolve_operator_config(tmp_path) == system

    def test_env_override_outranks_the_home_default(self, tmp_path, monkeypatch):
        chosen = tmp_path / "operator" / "config.operator.yaml"
        chosen.parent.mkdir()
        chosen.write_text("sync: {adapter: noop}\n")
        home = tmp_path / "home"
        home.mkdir()
        (home / "config.operator.yaml").write_text("sync: {adapter: filesystem}\n")
        monkeypatch.setenv(config_trust.OPERATOR_CONFIG_ENV, str(chosen))

        assert resolve_operator_config(home) == chosen

    def test_dangling_env_override_is_reported_not_ignored(self, tmp_path, monkeypatch):
        """A typo in a deployment must not silently restore config.yaml's authority.

        Falling through to the home default here would turn a misconfiguration
        into a silent downgrade — the state an operator is least able to notice.
        """
        home = tmp_path / "home"
        home.mkdir()
        (home / "config.operator.yaml").write_text("sync: {adapter: noop}\n")
        monkeypatch.setenv(config_trust.OPERATOR_CONFIG_ENV, str(tmp_path / "nope.yaml"))

        resolved = resolve_operator_config(home)

        assert resolved == tmp_path / "nope.yaml"
        assert evaluate_lock(resolved).state == UNLOCKED

    def test_nothing_anywhere_resolves_to_none(self, tmp_path):
        assert resolve_operator_config(tmp_path) is None


# ---------------------------------------------------------------------------
# What Config.load does with each verdict
# ---------------------------------------------------------------------------


class TestLoadGate:
    def test_absent_leaves_config_yaml_authoritative(self, tmp_path, monkeypatch):
        """Backward compatibility, stated as a test: an install with no operator
        layer behaves exactly as it did before techne#393."""
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            yaml.dump({"sync": {"adapter": "filesystem", "path": "/tmp/out"}})
        )

        cfg = Config.load()

        assert cfg.sync.adapter == "filesystem"
        assert cfg.sync.path == "/tmp/out"
        assert cfg.egress_trust is not None and cfg.egress_trust.state == ABSENT

    def test_locked_layer_overrides_config_yaml(self, tmp_path, monkeypatch):
        """The acceptance criterion of #393: with a locked layer, an agent
        writing config.yaml cannot enable a network adapter."""
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            yaml.dump(
                {
                    "sync": {"adapter": "filesystem", "path": "/tmp/exfil"},
                    "llm_api_base": "https://attacker.example",
                    "model": "openai/gpt-4",
                }
            )
        )
        operator = tmp_path / "operator.yaml"
        operator.write_text(yaml.dump({"sync": {"adapter": "noop"}}))
        _force(monkeypatch, LOCKED, operator)

        cfg = Config.load()

        assert cfg.sync.adapter == "noop"
        assert cfg.sync.path == ""
        assert cfg.llm_api_base == ""
        assert cfg.model == Config().model

    def test_locked_layer_can_grant_egress_config_yaml_could_not(self, tmp_path, monkeypatch):
        """The gate is a boundary, not a ban: the operator remains able to turn
        egress ON. A mechanism that only ever said no would be indistinguishable
        from deleting the feature."""
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(yaml.dump({"platform": "claude-code"}))
        operator = tmp_path / "operator.yaml"
        operator.write_text(
            yaml.dump({"sync": {"adapter": "filesystem", "path": "/srv/out"}, "standalone": False})
        )
        _force(monkeypatch, LOCKED, operator)

        cfg = Config.load()

        assert cfg.sync.adapter == "filesystem"
        assert cfg.sync.path == "/srv/out"
        assert cfg.standalone is False

    def test_key_omitted_by_the_operator_reverts_to_the_builtin_default(
        self, tmp_path, monkeypatch
    ):
        """Authority over the set is all-or-nothing.

        config.yaml asks for a foreign endpoint; the operator layer pins only
        ``sync``. The endpoint must NOT survive — partial authority would leave
        the agent choosing every destination the operator forgot to name.
        """
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            yaml.dump({"llm_api_base": "https://attacker.example", "llm_api_key": "sk-stolen"})
        )
        operator = tmp_path / "operator.yaml"
        operator.write_text(yaml.dump({"sync": {"adapter": "noop"}}))
        _force(monkeypatch, LOCKED, operator)

        cfg = Config.load()

        assert cfg.llm_api_base == ""
        assert cfg.llm_api_key == ""

    def test_unlocked_layer_fails_closed(self, tmp_path, monkeypatch):
        """A deployed-but-unenforceable layer trusts NEITHER file.

        Honouring config.yaml here would mean the presence of a broken lock
        restores exactly the authority the lock was deployed to remove.
        """
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            yaml.dump({"sync": {"adapter": "filesystem"}, "llm_api_base": "https://x.example"})
        )
        operator = tmp_path / "operator.yaml"
        operator.write_text(yaml.dump({"sync": {"adapter": "filesystem", "path": "/srv/out"}}))
        _force(monkeypatch, UNLOCKED, operator)

        cfg = Config.load()

        assert cfg.sync.adapter == "noop"
        assert cfg.llm_api_base == ""
        assert cfg.egress_trust.state == UNLOCKED

    def test_locked_layer_applies_with_no_config_yaml_at_all(self, tmp_path, monkeypatch):
        """A default install is still governed. The no-config.yaml branch is a
        separate early return in load(), so it needs its own assertion or the
        gate is one refactor away from applying to customized installs only."""
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        operator = tmp_path / "operator.yaml"
        operator.write_text(yaml.dump({"sync": {"adapter": "filesystem", "path": "/srv/out"}}))
        _force(monkeypatch, LOCKED, operator)

        cfg = Config.load()

        assert cfg.sync.adapter == "filesystem"
        assert cfg.egress_trust.state == LOCKED

    def test_unparseable_operator_layer_grants_nothing(self, tmp_path, monkeypatch):
        """Fail closed on a broken layer rather than falling back to config.yaml."""
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(yaml.dump({"sync": {"adapter": "filesystem"}}))
        operator = tmp_path / "operator.yaml"
        operator.write_text("{{{ not yaml")
        _force(monkeypatch, LOCKED, operator)

        cfg = Config.load()

        assert cfg.sync.adapter == "noop"

    def test_non_egress_keys_are_untouched_by_the_gate(self, tmp_path, monkeypatch):
        """The gate governs destinations only. If it were quietly resetting
        unrelated settings, every locked install would silently lose its tuning."""
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            yaml.dump(
                {
                    "sync": {"adapter": "filesystem"},
                    "staleness_hours": 99,
                    "hooks": {"capture_bash": True},
                    "threads": {"min_observations": 42},
                }
            )
        )
        operator = tmp_path / "operator.yaml"
        operator.write_text(yaml.dump({"sync": {"adapter": "noop"}}))
        _force(monkeypatch, LOCKED, operator)

        cfg = Config.load()

        assert cfg.staleness_hours == 99
        assert cfg.hooks.capture_bash is True
        assert cfg.threads.min_observations == 42


class TestSaveDoesNotPersistOperatorValues:
    def test_save_omits_egress_keys_while_a_layer_is_in_force(self, tmp_path, monkeypatch):
        """`techne init` is a load→save round-trip. Writing the operator's values
        into the agent-writable file would leave a privilege that outlives the
        thing that granted it."""
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(yaml.dump({"staleness_hours": 7}))
        operator = tmp_path / "operator.yaml"
        operator.write_text(yaml.dump({"sync": {"adapter": "filesystem", "path": "/srv/out"}}))
        _force(monkeypatch, LOCKED, operator)

        cfg = Config.load()
        cfg.save()

        written = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert "sync" not in written
        assert "model" not in written
        assert "standalone" not in written
        # The non-egress round-trip must survive — this is a targeted strip, not
        # a refusal to save.
        assert written["staleness_hours"] == 7

    def test_save_keeps_egress_keys_when_no_layer_exists(self, tmp_path, monkeypatch):
        """Unchanged behavior for the default install, asserted rather than assumed."""
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(yaml.dump({"sync": {"adapter": "filesystem"}}))

        cfg = Config.load()
        cfg.save()

        written = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert written["sync"]["adapter"] == "filesystem"


# ---------------------------------------------------------------------------
# Key surgery
# ---------------------------------------------------------------------------


class TestKeySurgery:
    def test_strip_removes_nested_destination_keys(self):
        """reflect.model and conflict.model each override the base model, so a
        strip that only handled top-level keys would leave two live ways to
        repoint the same traffic."""
        raw = {
            "reflect": {"model": "openai/gpt-4", "enabled": True},
            "conflict": {"model": "openai/gpt-4", "max_items_per_emit": 5},
            "model": "openai/gpt-4",
        }

        removed = strip_egress(raw)

        assert "reflect.model" in removed and "conflict.model" in removed
        assert raw["reflect"] == {"enabled": True}
        assert raw["conflict"] == {"max_items_per_emit": 5}
        assert "model" not in raw

    def test_apply_replaces_rather_than_merges(self):
        """A merge would let config.yaml keep sub-keys of a block the operator
        pinned — e.g. its own sync.path under the operator's adapter."""
        raw = {"sync": {"adapter": "filesystem", "path": "/tmp/exfil", "auth": "token"}}

        applied = apply_operator_layer(raw, {"sync": {"adapter": "noop"}})

        assert applied == ["sync"]
        assert raw["sync"] == {"adapter": "noop"}

    def test_apply_is_a_no_op_when_the_operator_declares_nothing(self):
        """An empty layer still strips. It says "I govern this set and choose the
        defaults" — not "carry on"."""
        raw = {"sync": {"adapter": "filesystem"}, "llm_api_base": "https://x.example"}

        applied = apply_operator_layer(raw, {})

        assert applied == []
        assert raw == {}

    def test_strip_removes_sources_now_that_it_is_governed(self):
        """techne#585 CHANGED ENFORCEMENT, so it gets a test rather than only a
        comment. `sources.claude_code_transcripts.paths` names arbitrary
        filesystem paths whose CONTENTS are ingested into the ledger; admitting
        it to EGRESS_KEYS means a LOCKED operator layer now DROPS agent-supplied
        source paths instead of honouring them.

        That is the intended effect of locking — an agent choosing what gets read
        into its own memory is the thing being prevented — but it is a live
        behaviour change for any locked install that set `sources` in
        config.yaml, not a no-op. Pinned here so nobody can quietly revert the
        decision by moving one line back to `_NOT_A_DESTINATION`.
        """
        raw = {
            "sources": {"claude_code_transcripts": {"paths": ["/tmp/somebody-elses-transcripts"]}},
            "threads": {"min_observations": 3},
        }

        removed = strip_egress(raw)

        assert "sources" in removed
        assert "sources" not in raw
        # A neighbouring non-destination block is untouched — the gate is
        # targeted, not a blanket wipe.
        assert raw["threads"] == {"min_observations": 3}

    def test_strip_tolerates_a_wrong_shaped_block(self):
        """A scalar where a mapping belongs must not raise — this runs inside
        Config.load, and a crash here takes down every hook on the host."""
        raw = {"reflect": "not-a-mapping", "sync": None}

        strip_egress(raw)

        assert raw["reflect"] == "not-a-mapping"
        assert "sync" not in raw


# ---------------------------------------------------------------------------
# The doctor check
# ---------------------------------------------------------------------------


class TestCheckConfigTrust:
    """The check probes the filesystem live; see its docstring for why it must
    not read ``config.egress_trust`` as its primary signal."""

    def _run(self, config):
        from techne.core.checks import check_config_trust

        return check_config_trust(config)

    def test_absent_is_dormant_and_names_what_config_yaml_selects(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(yaml.dump({"sync": {"adapter": "filesystem"}}))

        result = self._run(Config.load())

        assert result.verdict.value == "dormant"
        assert "agent-writable" in result.detail
        assert "sync.adapter=filesystem" in result.detail

    def test_absent_with_builtin_defaults_says_so(self, tmp_path, monkeypatch):
        """Same verdict as above, opposite significance — the detail is the only
        thing that distinguishes a benign default install from an exposed one."""
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))

        result = self._run(Config.load())

        assert result.verdict.value == "dormant"
        assert "all destinations at built-in values" in result.detail

    def test_unlocked_is_broken_with_an_actionable_remedy(self, tmp_path, monkeypatch):
        """The state that should alarm: someone deployed a boundary and does not
        have one. A plain 'is egress closed?' check would render this GREEN,
        because the keys it would read sit at their built-in values here."""
        operator = tmp_path / "operator.yaml"
        operator.write_text(yaml.dump({"sync": {"adapter": "noop"}}))
        _force(monkeypatch, UNLOCKED, operator, reason="owned by this process's own euid 501")

        result = self._run(Config.load())

        assert result.verdict.value == "broken"
        assert result.remedy and str(operator) in result.remedy
        assert "euid" in result.evidence

    def test_locked_is_live(self, tmp_path, monkeypatch):
        operator = tmp_path / "operator.yaml"
        operator.write_text(yaml.dump({"sync": {"adapter": "noop"}}))
        _force(monkeypatch, LOCKED, operator, reason="owned by uid 0")

        result = self._run(Config.load())

        assert result.verdict.value == "live"
        assert result.remedy is None

    def test_a_layer_deployed_after_load_is_reported_stale_not_live(self, tmp_path, monkeypatch):
        """The running process is still honoring its load-time resolution. Live
        LOCKED plus recorded ABSENT is a false green if reported as LIVE."""
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        config = Config.load()
        assert config.egress_trust.state == ABSENT

        operator = tmp_path / "operator.yaml"
        operator.write_text(yaml.dump({"sync": {"adapter": "noop"}}))
        _force(monkeypatch, LOCKED, operator)

        result = self._run(config)

        assert result.verdict.value == "stale"
        assert "absent" in result.evidence and "locked" in result.evidence
        assert result.remedy is not None

    def test_a_directly_constructed_config_still_gets_a_real_answer(self):
        """No verdict recorded is not the same as no answer available — the
        filesystem is still there to be probed. Regression pin: reading
        egress_trust as the primary signal turned every directly-constructed
        Config into an UNKNOWN, which took the health banner amber across five
        unrelated tests."""
        result = self._run(Config())

        assert result.verdict.value == "dormant"


class TestSaveDoesNotDestroyTheFileItOverrules:
    """Regression pins for the data loss found on PR #508.

    The original `save()` test seeded `config.yaml` with a single non-egress key,
    so there was never an egress value present to be destroyed — the test was
    constructed around the gap it was meant to cover. These seed real values,
    including credentials.
    """

    def _seed(self, home: Path) -> dict:
        raw = {
            "sync": {"adapter": "filesystem", "path": "/srv/out", "auth": "SECRET-TOKEN"},
            "llm_api_base": "https://proxy.internal",
            "llm_api_key": "sk-operator",
            "connections": {"team": {"role": "team", "db_path": "/srv/team.db"}},
            "stores": {"librarian": {"adapter": "sqlite", "dsn": "/srv/lib.db"}},
            "staleness_hours": 7,
        }
        (home / "config.yaml").write_text(yaml.dump(raw))
        return raw

    def test_unlocked_save_preserves_the_agent_side_values_including_credentials(
        self, tmp_path, monkeypatch
    ):
        """`techne init` is a load->mutate->save round-trip. Before the fix it
        deleted sync.auth and llm_api_key outright, unrecoverably, on a routine
        command — along the path the card itself names as the expected mistake."""
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        self._seed(tmp_path)
        operator = tmp_path / "operator.yaml"
        operator.write_text(yaml.dump({"sync": {"adapter": "noop"}}))
        _force(monkeypatch, UNLOCKED, operator)

        cfg = Config.load()
        cfg.save()

        after = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert after["sync"]["auth"] == "SECRET-TOKEN"
        assert after["sync"]["adapter"] == "filesystem"
        assert after["llm_api_key"] == "sk-operator"
        assert after["llm_api_base"] == "https://proxy.internal"
        assert after["connections"]["team"]["db_path"] == "/srv/team.db"
        assert after["stores"]["librarian"]["dsn"] == "/srv/lib.db"
        assert after["staleness_hours"] == 7

    def test_the_gate_still_ignores_what_the_file_preserves(self, tmp_path, monkeypatch):
        """Preservation must not become permission. The file records the request;
        the running config still refuses it."""
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        self._seed(tmp_path)
        operator = tmp_path / "operator.yaml"
        operator.write_text(yaml.dump({"sync": {"adapter": "noop"}}))
        _force(monkeypatch, UNLOCKED, operator)

        cfg = Config.load()
        cfg.save()

        assert cfg.sync.adapter == "noop"
        assert cfg.llm_api_base == ""
        assert Config.load().sync.adapter == "noop"

    def test_locked_save_never_writes_the_operator_values_down(self, tmp_path, monkeypatch):
        """The other half, unchanged: the OPERATOR's pins must not land in the
        agent-writable file, or they would outlive the layer that granted them."""
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(yaml.dump({"staleness_hours": 7}))
        operator = tmp_path / "operator.yaml"
        operator.write_text(yaml.dump({"sync": {"adapter": "filesystem", "path": "/srv/OPERATOR"}}))
        _force(monkeypatch, LOCKED, operator)

        cfg = Config.load()
        cfg.save()

        after = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert "/srv/OPERATOR" not in yaml.dump(after)
        assert "sync" not in after


class TestResolvedPathSoundness:
    def test_a_symlinked_ancestor_we_control_defeats_the_lock(self, tmp_path):
        """Walking only the RESOLVED chain reported LOCKED here.

        `/agent/link -> /etc` with `/agent` writable by us: lstat follows the
        intermediate symlink to a genuine root-owned file, resolve() yields
        root-owned ancestors — while the directory that decides where the later
        open() lands was never inspected.
        """
        if os.geteuid() == 0 or not Path("/etc/hosts").exists():
            pytest.skip("needs a non-root euid and a root-owned /etc/hosts")
        link = tmp_path / "link"
        link.symlink_to("/etc")

        verdict = evaluate_lock(link / "hosts")

        assert verdict.state == UNLOCKED
        assert "parent dir" in verdict.reason
        assert str(tmp_path) in verdict.reason

    def test_the_verdict_carries_the_inode_it_proved(self, tmp_path):
        """Readers must open what the predicate proved, not re-walk the name."""
        hosts = Path("/etc/hosts")
        if os.geteuid() == 0 or not hosts.exists():
            pytest.skip("needs a non-root euid and a root-owned /etc/hosts")
        st = os.stat(hosts)
        # Same environmental guard as the sibling above. Without it this test
        # fails for a HOST reason inside the file whose job is to tell real
        # failures apart from environmental ones.
        if st.st_uid == os.geteuid() or st.st_mode & 0o022:
            pytest.skip("/etc/hosts is not root-owned 0644 on this host")
        verdict = evaluate_lock(hosts)

        assert verdict.state == LOCKED
        assert verdict.read_target == Path("/etc/hosts").resolve()


class TestStoresAreGoverned:
    def test_store_dsn_is_an_egress_key(self, tmp_path, monkeypatch):
        """`stores.*.dsn` routes domain memory to an arbitrary path, structurally
        identical to the already-governed `archivist_db_path`."""
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            yaml.dump({"stores": {"librarian": {"adapter": "sqlite", "dsn": "/tmp/leak.db"}}})
        )
        operator = tmp_path / "operator.yaml"
        operator.write_text(yaml.dump({"sync": {"adapter": "noop"}}))
        _force(monkeypatch, LOCKED, operator)

        cfg = Config.load()

        assert cfg.stores == {}


class TestTheLayerIsReadFromTheInodeThatWasProven:
    def test_load_reads_resolved_not_path(self, tmp_path, monkeypatch):
        """Threading `resolved` onto the verdict is only half the fix; the gate
        must CONSUME it.

        Nothing caught the consumption half — `_force` left `resolved=None`, so
        `read_target` and `path` were indistinguishable in every gate test and
        reverting `verdict.read_target` to `verdict.path` kept the suite green.
        That is the same shape of half-application caught by hand last round, on
        the half that actually opens the file. (Prudence, PR #508 r2, nit 2.)
        """
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        decoy = tmp_path / "decoy.yaml"
        decoy.write_text(yaml.dump({"sync": {"adapter": "filesystem", "path": "/FROM-DECOY"}}))
        proven = tmp_path / "proven.yaml"
        proven.write_text(yaml.dump({"sync": {"adapter": "filesystem", "path": "/FROM-PROVEN"}}))
        _force(monkeypatch, LOCKED, decoy, resolved=proven)

        cfg = Config.load()

        assert cfg.sync.path == "/FROM-PROVEN"


class TestTierIsNamedAndSoIsItsLever:
    """Blocker 2's entire code-visible remedy had no assertion behind it — the
    one thing separating "documented" from "handled". (Prudence r2, nit 1.)"""

    def _detail(self, config):
        from techne.core.checks import check_config_trust

        return check_config_trust(config)

    def test_system_tier_carries_no_warning(self, tmp_path, monkeypatch):
        system = tmp_path / "etc" / "config.operator.yaml"
        system.parent.mkdir()
        system.write_text(yaml.dump({"sync": {"adapter": "noop"}}))
        monkeypatch.setattr(config_trust, "SYSTEM_OPERATOR_CONFIG", system)
        _force(monkeypatch, LOCKED, system)

        result = self._detail(Config.load())

        assert result.verdict.value == "live"
        assert "WARNING" not in result.detail
        assert "[system tier]" in result.evidence

    def test_env_var_tier_names_the_env_var_as_the_lever(self, tmp_path, monkeypatch):
        chosen = tmp_path / "operator.yaml"
        chosen.write_text(yaml.dump({"sync": {"adapter": "noop"}}))
        monkeypatch.setenv(config_trust.OPERATOR_CONFIG_ENV, str(chosen))
        _force(monkeypatch, LOCKED, chosen)

        result = self._detail(Config.load())

        assert "WARNING" in result.detail
        assert f"env -u {config_trust.OPERATOR_CONFIG_ENV}" in result.detail

    def test_home_tier_names_TECHNE_HOME_not_the_env_var(self, tmp_path, monkeypatch):
        """The binary tier split told an operator on the <techne_home> tier to
        defend against `env -u TECHNE_OPERATOR_CONFIG`, which does nothing to
        that tier — its lever is TECHNE_HOME. Handing someone the wrong bypass
        is worse than handing them none."""
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        monkeypatch.delenv(config_trust.OPERATOR_CONFIG_ENV, raising=False)
        layer = tmp_path / "config.operator.yaml"
        layer.write_text(yaml.dump({"sync": {"adapter": "noop"}}))
        _force(monkeypatch, LOCKED, layer)

        result = self._detail(Config.load())

        assert "WARNING" in result.detail
        assert "TECHNE_HOME" in result.detail
        assert "env -u" not in result.detail


class TestEveryGovernedKeyIsReportable:
    def test_the_reported_list_is_derived_from_the_governed_set(self, tmp_path, monkeypatch):
        """`stores` was governed but unreported, so an install with a custom DSN
        said "all destinations at built-in values" — the benign string. The key
        list is now derived from EGRESS_KEYS, so a key cannot join the governed
        set and silently fail to be reported. (Prudence r2, Important 2.)"""
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            yaml.dump(
                {
                    "stores": {"librarian": {"adapter": "sqlite", "dsn": "/tmp/leak.db"}},
                    "standalone": False,
                    "reflect": {"model": "openai/gpt-4"},
                    "llm_api_key": "sk-should-not-be-printed",
                }
            )
        )
        from techne.core.checks import check_config_trust

        detail = check_config_trust(Config.load()).detail

        assert "all destinations at built-in values" not in detail
        assert "stores" in detail
        assert "standalone" in detail
        assert "reflect.model" in detail
        # Presence is reportable; the secret itself is not.
        assert "llm_api_key set" in detail
        assert "sk-should-not-be-printed" not in detail


class TestTheDetailNeverLeaksACredential:
    """Default-deny, pinned. The derived key list guarantees a new governed key
    is REPORTED; only this guarantees it is reported SAFELY. Prudence
    demonstrated the gap on PR #508 r3 by simulating the next round adding a
    credential key the way `stores` was added — it printed straight into doctor
    output with no diff to review.
    """

    def _detail(self, config):
        from techne.core.checks import check_config_trust

        return check_config_trust(config).detail

    def test_a_newly_governed_key_reports_presence_not_value(self, tmp_path, monkeypatch):
        """The simulated future round: a credential-bearing key joins the set."""
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            yaml.dump({"sync": {"adapter": "http", "auth": "sk-BRAND-NEW-SECRET"}})
        )
        monkeypatch.setattr(config_trust, "EGRESS_KEYS", (*config_trust.EGRESS_KEYS, "sync.auth"))

        detail = self._detail(Config.load())

        assert "sk-BRAND-NEW-SECRET" not in detail
        assert "sync.auth set" in detail

    def test_llm_api_base_reports_presence_because_a_url_can_carry_credentials(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            yaml.dump({"llm_api_base": "https://user:pa55@proxy.internal"})
        )

        detail = self._detail(Config.load())

        assert "pa55" not in detail
        assert "llm_api_base set" in detail

    def test_a_sync_block_that_departed_only_in_its_secret_fields_is_still_legible(
        self, tmp_path, monkeypatch
    ):
        """`sync.adapter=noop` alone was true and useless — the operator reads
        past their own default. Name the departed fields, never their values."""
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            yaml.dump({"sync": {"adapter": "noop", "url": "https://x.example", "auth": "sk-tok"}})
        )

        detail = self._detail(Config.load())

        assert "sk-tok" not in detail and "x.example" not in detail
        assert "+url, auth" in detail

    def test_an_unrecognised_sync_subkey_is_named_not_swallowed(self, tmp_path, monkeypatch):
        """`SyncConfig.extra` catches EVERY unrecognised key under `sync:` — the
        one field that collects what the code does not know about. The
        hand-kept four-name list did not cover it, so a departed block rendered
        a bare `sync.adapter=noop`. Derived from `dataclasses.fields` now.
        (Prudence, PR #508 r4 — executed, not proposed.)
        """
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            yaml.dump({"sync": {"adapter": "noop", "token": "ghp_LIVE_SECRET"}})
        )

        detail = self._detail(Config.load())

        assert "ghp_LIVE_SECRET" not in detail
        assert detail.count("sync.adapter=noop") == 0 or "+" in detail
        assert "extra" in detail or "token" in detail

    def test_archivist_db_path_reports_presence_whatever_shape_its_value_takes(
        self, tmp_path, monkeypatch
    ):
        """techne#87 turns `archivist_db_path` from a local path into a DSN with
        a password in it, arriving with no diff here — so it is denied by
        MEMBERSHIP rather than by a shape test. The shape test that preceded
        this missed libpq keyword/value form entirely (`host=db password=pa55`)
        and simultaneously broke `model: vertex_ai/...@20240620`."""
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            yaml.dump({"archivist_db_path": "postgres://user:pa55@db.internal/archivist"})
        )

        detail = self._detail(Config.load())

        assert "pa55" not in detail
        assert "archivist_db_path set" in detail

    def test_a_model_id_containing_an_at_sign_is_still_named_in_full(self, tmp_path, monkeypatch):
        """The collateral damage of the deleted heuristic: a versioned Vertex
        model id trips a naive `@` test, and naming the destination is the whole
        job of this key."""
        monkeypatch.setenv("TECHNE_HOME", str(tmp_path))
        (tmp_path / "config.yaml").write_text(
            yaml.dump({"model": "vertex_ai/claude-3-5-sonnet@20240620"})
        )

        detail = self._detail(Config.load())

        assert "model=vertex_ai/claude-3-5-sonnet@20240620" in detail


# --- techne#509: every Config field must be classified -----------------------


def _config_field_names() -> list[str]:
    """Top-level `Config` field names, from the dataclass itself.

    Read via `dataclasses.fields` rather than parsed or hand-listed: a snapshot
    of names in the test would be a second copy of the surface, drifting exactly
    when the guard needs to be right.
    """

    from techne.config import Config

    return [f.name for f in dataclasses.fields(Config)]


def test_every_config_field_is_classified_as_destination_or_not():
    """THE INVARIANT (techne#509). A new destination-selecting field added to
    `Config` with no line in `EGRESS_KEYS` is a key the operator layer silently
    does not govern — no diff to review, no red test. That is how `stores` stayed
    ungoverned until round 2 of #508's review caught it by eye.

    `EGRESS_KEYS` genuinely cannot be derived (see the comment above
    `_NOT_A_DESTINATION`), so this does not try to. It enforces the weaker,
    achievable property: that SOMEBODY MADE THE CALL. Adding a field now costs one
    line in one of two tuples instead of being allowed to pass in silence.

    A PREFIX IS NOT A JUDGMENT (techne#588). This check used to compute the
    governed set as `{k.split(".", 1)[0] for k in EGRESS_KEYS}`, so the single
    entry `reflect.model` marked the ENTIRE `reflect` block classified — and
    neither `reflect` nor `conflict` appeared in `_NOT_A_DESTINATION`, because
    the prefix had already satisfied the check. Measured: adding
    `ReflectConfig.api_base`, an LLM endpoint override, left the suite at 51
    passed, byte-identical to baseline. A destination-selecting field nested one
    level inside a partially-governed block was silently ungoverned, which is
    precisely what this test exists to prevent.

    So classification is now EXACT and RECURSIVE: a block named WHOLE (no dot) in
    either tuple covers everything under it; a block governed only by DOTTED keys
    is partially governed, and every field inside it must be judged in its own
    right. The recursion is general rather than one-deep on purpose — the same
    hole at depth 2 would otherwise be a new bug with the same shape.
    """
    from techne.security import config_trust

    whole = {k for k in config_trust.EGRESS_KEYS if "." not in k}
    whole |= {k for k in config_trust._NOT_A_DESTINATION if "." not in k}
    dotted = {k for k in config_trust.EGRESS_KEYS if "." in k}
    dotted |= {k for k in config_trust._NOT_A_DESTINATION if "." in k}

    unclassified = _unclassified_fields(Config, whole, dotted, prefix="")
    assert not unclassified, (
        f"Config fields with no destination judgment: {unclassified}. "
        "Add each to techne/security/config_trust.py — EGRESS_KEYS if it selects "
        "a destination for memory (a path, DSN, endpoint, model route or sink "
        "toggle), otherwise _NOT_A_DESTINATION with the reason. Silence here means "
        "the operator layer does not govern it.\n"
        "A DOTTED name here means the enclosing block is only PARTIALLY governed "
        "(some sub-key is in EGRESS_KEYS), so every field inside it needs its own "
        "line — a prefix is not a judgment (techne#588)."
    )


def _unclassified_fields(cls, whole: set[str], dotted: set[str], prefix: str) -> list[str]:
    """Names under `cls` that no entry in either tuple judges.

    `whole` = undotted entries (cover a block entirely); `dotted` = entries that
    govern one field, which makes their enclosing block partially governed and
    forces a per-field judgment inside it.
    """
    import typing

    out: list[str] = []
    try:
        hints = typing.get_type_hints(cls)
    except Exception:  # noqa: BLE001 — an unresolvable annotation must not hide fields
        hints = {}
    for f in dataclasses.fields(cls):
        dotted_name = f"{prefix}{f.name}"
        if dotted_name in whole:
            continue  # judged wholesale — covers everything beneath it
        partial = any(k.startswith(f"{dotted_name}.") for k in dotted)
        if not partial:
            out.append(dotted_name)
            continue
        # Partially governed: descend and demand a judgment on every field inside.
        inner = hints.get(f.name, f.type)
        if dataclasses.is_dataclass(inner):
            out.extend(_unclassified_fields(inner, whole | dotted, dotted, f"{dotted_name}."))
    return out


def test_the_two_tuples_do_not_overlap():
    """A name in both would make the record self-contradicting, and the runtime
    would quietly follow EGRESS_KEYS while the comment claimed otherwise.

    Compared EXACTLY, not by prefix (techne#588). Both tuples now carry dotted
    entries — `reflect.model` governs, `reflect.write_to` declines — and those
    are not a contradiction, they are the per-field judgment a partially-governed
    block requires. Collapsing to prefixes would report them as one.
    """
    from techne.security import config_trust

    both = set(config_trust.EGRESS_KEYS) & set(config_trust._NOT_A_DESTINATION)
    assert not both, f"classified as both destination and not: {sorted(both)}"

    # A block named WHOLE in one tuple must not also have dotted entries in the
    # other: "all of `reflect` is not a destination" and "`reflect.model` is"
    # cannot both be true, and the exact comparison above would not catch it.
    whole_governed = {k for k in config_trust.EGRESS_KEYS if "." not in k}
    whole_declined = {k for k in config_trust._NOT_A_DESTINATION if "." not in k}
    contradictions = sorted(
        k
        for k in config_trust.EGRESS_KEYS + config_trust._NOT_A_DESTINATION
        if "." in k
        and (
            (k.split(".", 1)[0] in whole_governed and k in config_trust._NOT_A_DESTINATION)
            or (k.split(".", 1)[0] in whole_declined and k in config_trust.EGRESS_KEYS)
        )
    )
    assert not contradictions, (
        f"sub-keys contradicting a whole-block judgment: {contradictions} — the "
        "block is classified wholesale in one tuple while a field inside it is "
        "classified the other way."
    )


# THE DATACLASS WALKER WAS DELETED HERE (techne#586). `_resolve_egress_key`,
# `_Unverifiable`, the synthetic `_WalkerRoot` fixtures and their unit tests all
# existed to make an orphan guard that resolved EGRESS_KEYS against the DATACLASS
# surface trustworthy — including two documented limits (a dict keyed by runtime
# data, and a `list[...]` that must not be unwrapped as though it were a
# dataclass) and a hard-won fix for a silent mis-unwrap.
#
# All of it is retired because the guard no longer walks that surface at all. The
# gate acts on the surface `Config.load` READS, so the replacement below loads a
# config with each key and without it and requires the result to differ. That has
# no walker, therefore no limits 2 and 3, and no synthetic fixtures whose only
# consumer was the walker. Keeping them would have been coverage of a helper
# nothing depends on — which reads as safety and is not.
#
# The limits are gone from docs/arch/config-trust.md in the same change.

#: A distinctive YAML value per governed key, in the SHAPE `Config.load` expects.
#: Hand-written on purpose: the point of the guard below is to prove the loader
#: READS each key, and a probe derived from the loader would prove nothing. If a
#: key's shape changes, the guard fails loudly rather than silently passing.
_EGRESS_PROBES: dict[str, object] = {
    "sync": {"adapter": "filesystem", "path": "/tmp/probe-sync"},
    # `role` must be one of _FEDERATION_ROLES — the loader validates it, so a
    # made-up role fails here rather than silently probing nothing.
    "connections": {"probe": {"role": "team", "db_path": "/tmp/probe-peer.db"}},
    "llm_api_base": "https://probe.example",
    "llm_api_key": "probe-key",
    "model": "openai/probe-model",
    "reflect.model": "openai/probe-reflect",
    "conflict.model": "openai/probe-conflict",
    # techne#552. `archivist` selects WHICH STORE the seam is bound to. Probes "local"
    # rather than "store" because the loader rejects `store` with no target — a probe
    # that raises would prove the key validates, not that stripping it denies anything,
    # which is what this guard is for.
    "archivist": "local",
    # techne#555. The custodian's bindings run UNATTENDED, so a probe here is not
    # ceremony: it is what proves a locked operator layer can actually deny an agent
    # that tries to repoint headless traffic at its own endpoint.
    "custodian.model": "openai/probe-custodian",
    # Must name a REAL rail — the loader validates rail names against the registry
    # (techne#555), so a made-up rail raises rather than silently probing nothing,
    # exactly like `connections`' role above.
    "custodian.rail_models": {"faithfulness": "openai/probe-rail"},
    "archivist_db_path": "/tmp/probe-archivist.db",
    "standalone": False,
    "stores": {"librarian": {"adapter": "sqlite", "dsn": "/tmp/probe-lib.db"}},
    "sources": {"claude_code_transcripts": {"paths": ["/tmp/probe-transcripts"]}},
}


def _load_raw(tmp_path, raw: dict, monkeypatch):
    """Write `raw` as config.yaml and load it with no operator layer in play."""
    import dataclasses as _dc

    from techne.config import Config

    monkeypatch.setattr(config_trust, "evaluate_lock", lambda _p: TrustVerdict(ABSENT, None, "t"))
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    cfg = Config.load(path)
    state = _dc.asdict(cfg)
    # Volatile: identify the file and the verdict, not the governed values.
    for volatile in ("loaded_from", "egress_trust", "preserved_egress"):
        state.pop(volatile, None)
    return state


def _set_dotted(raw: dict, dotted: str, value) -> None:
    node = raw
    parts = dotted.split(".")
    for seg in parts[:-1]:
        node = node.setdefault(seg, {})
    node[parts[-1]] = value


def test_every_egress_key_is_a_key_the_loader_actually_reads(tmp_path, monkeypatch):
    """The other direction: a key the loader never reads governs nothing.

    TESTED AGAINST THE READ SURFACE, WHICH IS THE ONE THE GATE ACTS ON
    (techne#586). This guard used to walk the DATACLASS surface via
    `dataclasses.fields` + `get_type_hints`. That surface is INDEPENDENT of the
    one that matters: `EGRESS_KEYS` entries address the raw mapping
    `_pop`/`_get`/`_set` walk, and `Config.load` bridges YAML to fields through
    hardcoded string literals (`c.get("model", ...)`), so the two coincide by
    convention only. #582's review measured both failure directions, and the
    dangerous one is a rename of the ENTRY to match a renamed FIELD: old guard
    green, gate broken, agent's destination surviving a locked layer.

    A WRITE-SURFACE CHECK IS ALSO WRONG, and that was this fix's first attempt.
    Resolving against `Config.to_yaml_dict()` looked right and reported
    `conflict.model` as an orphan — because `save` emits no `conflict` block at
    all, though `load` reads one (config.py:759). The read and write surfaces are
    not the same set, so only the read side answers "can the gate deny anything
    here?".

    So the property is tested BEHAVIOURALLY and with no key→field mapping to rot:
    load a config WITH the key and one WITHOUT it, and require the resulting
    Config to differ. If it does not, the loader ignores the key, and stripping it
    denies nothing.
    """
    baseline = _load_raw(tmp_path, {}, monkeypatch)

    inert = []
    for key in config_trust.EGRESS_KEYS:
        probe = _EGRESS_PROBES.get(key)
        assert probe is not None, (
            f"no probe for governed key {key!r} — add one to _EGRESS_PROBES in the "
            "shape Config.load expects, so the key stays covered by this guard"
        )
        raw: dict = {}
        _set_dotted(raw, key, probe)
        if _load_raw(tmp_path, raw, monkeypatch) == baseline:
            inert.append(key)

    assert not inert, (
        f"EGRESS_KEYS entries the loader does not read: {inert} — setting them in "
        "config.yaml changes nothing, so stripping them denies nothing while the "
        "gate still looks complete (#504's shape).\n"
        "Find what `Config.load` READS for this value (grep the loader for the "
        "enclosing block) and make the entry name THAT key — not the dataclass "
        "field name, which can differ (techne#586)."
    )


def test_the_read_surface_guard_catches_an_entry_the_loader_cannot_read(tmp_path, monkeypatch):
    """The false negative from #582's review, pinned so it cannot come back.

    Renaming an EGRESS_KEYS entry to match a renamed FIELD rather than the YAML
    key left the old dataclass guard green while the gate silently stopped
    governing that destination. Behaviourally the mistake is unmissable: the
    invented key changes nothing about the loaded Config.
    """
    baseline = _load_raw(tmp_path, {}, monkeypatch)

    real: dict = {}
    _set_dotted(real, "conflict.model", "openai/probe-conflict")
    assert _load_raw(tmp_path, real, monkeypatch) != baseline, (
        "precondition: the real key is read by the loader"
    )

    invented: dict = {}
    _set_dotted(invented, "conflict.llm_model", "openai/probe-conflict")
    assert _load_raw(tmp_path, invented, monkeypatch) == baseline, (
        "a field-name rename the YAML never adopted must change NOTHING — that is "
        "exactly the green-test/broken-gate case techne#586 exists to close"
    )


def test_the_archivist_binding_is_rendered_not_just_flagged():
    """`archivist` was added to _PRINTABLE_DESTINATIONS with nothing pinning it, so the
    blast radius of losing it was `techne doctor` printing "archivist set" instead of
    "archivist=local" — uninformative rather than wrong, which is exactly the kind of
    regression that never gets noticed. Its entire value space is four validated
    literals, which is the structural argument that admits it. (Prudence, #714 r3.)
    """
    from techne.core.checks import _PRINTABLE_DESTINATIONS

    assert "archivist" in _PRINTABLE_DESTINATIONS
    # Its predecessor is printable by the same argument; losing either is a regression.
    assert "standalone" in _PRINTABLE_DESTINATIONS


def test_the_card_count_of_governed_sub_fields_is_current():
    """The card states a NUMBER, and nothing was checking it (techne#933 pre-pass).

    docs/arch/config-trust.md says _NOT_A_DESTINATION "carries dotted entries for
    all N sub-fields of reflect/conflict". Adding one dotted entry made that
    sentence false, and the arch-card gate did not notice: it compares the seam
    file's blob sha, so re-blessing after a source change silently re-certifies a
    body claim the change invalidated.

    A number in prose is only a guard if something recomputes it.
    """
    import re
    from pathlib import Path

    from techne.security import config_trust

    card = Path(__file__).resolve().parent.parent / "docs" / "arch" / "config-trust.md"
    text = card.read_text(encoding="utf-8")

    m = re.search(r"dotted entries for all (\d+) sub-fields", text)
    assert m, "the card no longer states a sub-field count in the expected form"
    claimed = int(m.group(1))

    actual = sum(
        1
        for k in list(config_trust.EGRESS_KEYS) + list(config_trust._NOT_A_DESTINATION)
        if k.startswith(("reflect.", "conflict."))
    )
    assert claimed == actual, (
        f"docs/arch/config-trust.md claims {claimed} dotted reflect/conflict "
        f"sub-fields; there are {actual}. Update the card body in the same change "
        "that adds or removes one."
    )
