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
    import dataclasses

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
    """
    from techne.security import config_trust

    # A dotted key like "reflect.model" governs part of the `reflect` field, so the
    # top-level name counts as classified by its prefix.
    governed = {k.split(".", 1)[0] for k in config_trust.EGRESS_KEYS}
    declined = set(config_trust._NOT_A_DESTINATION)

    unclassified = [f for f in _config_field_names() if f not in governed and f not in declined]
    assert not unclassified, (
        f"Config fields with no destination judgment: {unclassified}. "
        "Add each to techne/security/config_trust.py — EGRESS_KEYS if it selects "
        "a destination for memory (a path, DSN, endpoint, model route or sink "
        "toggle), otherwise _NOT_A_DESTINATION with the reason. Silence here means "
        "the operator layer does not govern it."
    )


def test_the_two_tuples_do_not_overlap():
    """A name in both would make the record self-contradicting, and the runtime
    would quietly follow EGRESS_KEYS while the comment claimed otherwise."""
    from techne.security import config_trust

    governed = {k.split(".", 1)[0] for k in config_trust.EGRESS_KEYS}
    both = governed & set(config_trust._NOT_A_DESTINATION)
    assert not both, f"classified as both destination and not: {sorted(both)}"


def _resolve_egress_key(key: str, root: object = None) -> str | None:
    """Walk a dotted EGRESS_KEYS entry through the real dataclass structure.

    Returns None when the key resolves, else the segment that broke it. `root`
    defaults to `Config` and exists so the tests below can exercise the walker's
    own edge cases against synthetic dataclasses — the real `Config` has no field
    in the shapes limits 2 and 3 describe, so without it those branches would be
    unreachable from the suite and pin nothing (#582 round 2 measured exactly
    that: mutating the UNVERIFIABLE branch away left the suite byte-identical).

    EVERY segment is resolved, not just the head. The runtime traverses the whole
    path (`_pop`/`_get`/`_set` recurse), so a check that stops at the head accepts
    `conflict.model` after `ConflictConfig.model` has been renamed — the key then
    governs nothing while the tuple still looks complete, which is the #504 shape
    this test exists to catch. Measured in #582's review: renaming
    `ConflictConfig.model` -> `llm_model` left the suite byte-identically green.

    THIS CHECKS THE DATACLASS SURFACE, AND THE GATE ACTS ON THE YAML SURFACE.
    They are INDEPENDENT and coincide only by convention: `Config.load` maps YAML
    to fields through hardcoded string literals (`c.get("model", ...)` at
    config.py:763), so renaming a field AND its loader kwarg while the YAML key
    stays put leaves the gate working while this check reports an orphan. The
    reverse is worse and is why the message below does not say "rename the entry
    to match the field": doing that yields `conflict.llm_model`, which #582's
    review MEASURED as test-green / gate-broken / agent's value surviving — this
    guard's remedy manufacturing the very shape it exists to catch.

    So this is a PROXY for the property that matters, not the property itself.
    It is kept because the surfaces do coincide today and drift between them is
    rarer than a plain rename, but a sound check would have to derive the keys
    the loader actually reads. Tracked in techne#586; do not read a pass here as
    "the gate governs this key".

    Raises `_Unverifiable` when a segment remains but the owner is no longer a
    dataclass (e.g. a `dict[str, ...]` whose keys are runtime data). Static
    resolution genuinely cannot settle those, and the caller reports them as such
    rather than counting them either way — a test that cannot see should say so.
    """
    import dataclasses
    import typing

    from techne.config import Config

    owner: object = Config if root is None else root
    for segment in key.split("."):
        if not dataclasses.is_dataclass(owner):
            raise _Unverifiable(key, segment)
        if segment not in {f.name for f in dataclasses.fields(owner)}:
            return segment
        hint = typing.get_type_hints(owner)[segment]
        # Unwrap `X | None` ONLY — otherwise a nested optional config stops the walk
        # one level short and reports a false orphan.
        #
        # THE UNWRAP IS CONDITIONED ON A None HAVING BEEN STRIPPED, not on arity.
        # `len(args) == 1` alone unwraps ANY single-argument generic, so
        # `list[StoreSpec]` resolved to `StoreSpec` and the walk continued as though
        # a list were a dataclass — silently, unlike the _Unverifiable path. Found in
        # #582's round-2 review; verified: list[StoreSpec] has 1 arg and no NoneType,
        # so it now falls through to _Unverifiable, which is the honest answer since
        # `_get`/`_pop` cannot traverse a list either.
        args = typing.get_args(hint)
        non_none = [a for a in args if a is not type(None)]
        owner = non_none[0] if len(non_none) == 1 and len(non_none) < len(args) else hint
    return None


class _Unverifiable(Exception):
    def __init__(self, key: str, segment: str) -> None:
        super().__init__(f"{key!r} (at {segment!r})")


@dataclasses.dataclass
class _WalkerLeaf:
    value: str = ""


@dataclasses.dataclass
class _WalkerRoot:
    """Synthetic root for the walker's edge cases. MODULE SCOPE ON PURPOSE:
    `get_type_hints` resolves annotations against the defining module's globals, so
    a dataclass declared inside a test function raises NameError on its own field
    types."""

    optional: _WalkerLeaf | None = None  # must STILL unwrap — not a limit
    listed: list[_WalkerLeaf] = dataclasses.field(default_factory=list)  # limit 3
    mapped: dict[str, _WalkerLeaf] = dataclasses.field(default_factory=dict)  # limit 2
    plain: _WalkerLeaf = dataclasses.field(default_factory=_WalkerLeaf)


def test_the_resolver_reports_its_own_blind_spots_instead_of_guessing():
    """PINS LIMITS 2 AND 3 of the orphan walker (docs/arch/config-trust.md).

    Both are unreachable through the real `Config`, which is why round 2 of #582's
    review could mutate the UNVERIFIABLE branch away and see a byte-identical green
    suite. A guard with no live case pins nothing — so these drive the walker
    directly, through `root`.

    LIMIT 3 IS THE ONE THAT USED TO FAIL SILENTLY. The unwrap was `args[0] if
    len(args) == 1`, which unwraps ANY single-argument generic, so a `list[Leaf]`
    resolved to `Leaf` and the walk continued as though a list were a dataclass.
    `_get`/`_pop` cannot traverse a list either, so the honest answer is
    UNVERIFIABLE, not a confident resolution.
    """
    Root = _WalkerRoot

    # The unwrap that must KEEP working — otherwise limit 3's fix would have been
    # bought by breaking optional nested configs, and this pair is what tells the
    # two apart.
    assert _resolve_egress_key("optional.value", root=Root) is None
    assert _resolve_egress_key("plain.value", root=Root) is None

    # A real miss is still reported as a miss, not swallowed as unverifiable.
    assert _resolve_egress_key("plain.gone", root=Root) == "gone"

    # Limit 3: a list is NOT a dataclass, and saying so is the fix.
    with pytest.raises(_Unverifiable):
        _resolve_egress_key("listed.value", root=Root)

    # Limit 2: a dict keyed by runtime data cannot be settled statically.
    with pytest.raises(_Unverifiable):
        _resolve_egress_key("mapped.value", root=Root)


def test_every_egress_key_still_names_a_real_config_field():
    """The other direction: a key that no longer matches a field governs nothing.

    A rename of `archivist_db_path` would leave EGRESS_KEYS pointing at a field
    that does not exist — the gate would still look complete and would pin
    nothing, which is #504's shape (mechanism present, not load-bearing).
    """
    from techne.security import config_trust

    orphans: dict[str, str] = {}
    unverifiable: list[str] = []
    for key in config_trust.EGRESS_KEYS:
        try:
            broken_at = _resolve_egress_key(key)
        except _Unverifiable as exc:
            unverifiable.append(str(exc))
            continue
        if broken_at is not None:
            orphans[key] = broken_at

    assert not orphans, (
        f"EGRESS_KEYS entries naming no Config field (key -> missing segment): {orphans} "
        "— a renamed or removed field leaves the gate pinning nothing while still "
        "looking complete.\n"
        "DO NOT simply rename the entry to match the new FIELD name. EGRESS_KEYS "
        "addresses the YAML surface (`_pop`/`_get` walk the raw dict), and "
        "`Config.load` maps YAML to fields through hardcoded literals, so the two "
        "can differ. Renaming the entry to a field the YAML does not use was "
        "MEASURED in #582's review as: this test green, the gate broken, the "
        "agent's value surviving a locked operator layer.\n"
        "Instead: find what `Config.load` reads for this value (grep the loader for "
        "the enclosing block) and make the entry name THAT key. If the YAML key is "
        "genuinely gone, drop the entry. See techne#586."
    )
    assert not unverifiable, (
        f"EGRESS_KEYS entries this test cannot statically resolve: {unverifiable}. "
        "A segment under a non-dataclass container (a dict keyed by runtime data) "
        "cannot be checked against the surface, so the orphan guard does not cover "
        "it. Either pin the container itself instead of a path through it, or add a "
        "test that exercises the traversal against a real loaded config."
    )
