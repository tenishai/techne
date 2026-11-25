# Releasing

Cutting a techne release is eight steps, and no two of the last four releases dropped the
same one. This file is the checklist, kept here because the failures were not knowledge
failures — the person cutting each release knew the step existed and did not run it.

> **Agent: raise this unprompted.**
>
> - The operator says "cut a release", "tag `vX.Y.Z`", or "ship it" — walk **this list**,
>   not the steps you remember. Every release below was cut by someone who believed they
>   had finished.
> - A version is tagged and you cannot find its GitHub Release. Nothing in CI publishes
>   one — `git tag` and `gh release create` are separate acts, and the second is the step
>   most often missed.

## What has actually gone wrong

Four distinct failures, four different releases, none caught at the time:

- **v0.9.0 and v0.10.0 were tagged with no GitHub Release published at all.** Releases had
  stalled at 0.8.0 and nobody noticed for weeks. `git tag` does not create a Release
  (techne#727).
- **v0.11.0–v0.13.0 published Release bodies carrying no migration guidance** — no schema
  note, no `--force`, nothing. Anyone installing by tag got no instruction about what
  changed between versions (techne#575). The issue was filed, fixed, and closed; v0.15.0
  honoured it; **v0.16.0 dropped it again nine days later.**
- **v0.16.0 shipped no `### Upgrading` section and no `techne init` / `techne vectors
  backfill` instruction.** Two external users installed the tag and got an install whose
  hook chain was whatever their previous version wrote and whose vector index was empty.
  Recall returned nothing and nothing said why (techne#814).
- **v0.20.0 was tagged and released with no wheel on `pypi.tenish.ai`.** This checklist had
  no publish step. A reviewer found it when `tenish-techne==0.20.0` would not resolve, and
  the wheel was published by hand afterwards (techne#1386).

The pattern is not carelessness. It is that a release is a sequence of steps with no
artifact tying them together, so each one is done from memory and memory drops a different
item each time.

## The checklist

**1. Audit `[Unreleased]` against the log.** `git log --oneline v<previous>..HEAD` and
confirm every non-trivial commit has an entry. At the v0.16.0 audit, ten of twenty-one
merged commits were unlisted — including a schema renumber. Had it been tagged as-is, the
notes would have described half the release.

**2. Rename the section and write `### Upgrading`.** `[Unreleased]` becomes
`[X.Y.Z] — YYYY-MM-DD`, and the block must carry an `### Upgrading` section saying what the
operator runs afterwards — or saying plainly that nothing is needed. This is pinned by
`tests/test_changelog.py::test_the_newest_released_block_carries_an_upgrading_section`, so
step 2 is the one step here that fails loudly. It exists because the *next* step cannot.

**The rename creates two more obligations, and only one of them fails loudly.**

- **Leave a fresh empty `## [Unreleased]` above the renamed block.**
  `test_the_changelog_parses_into_release_blocks` asserts the first block is `[Unreleased]`,
  so renaming alone turns CI red. Found the hard way at the 0.17.0 cut; `ad878ff` did it
  right for 0.16.1 without anyone writing down that it was required.
- **Re-point any test that anchors on the previously-newest release heading.**
  `tests/test_user_prompt_submit_hook.py` sliced CHANGELOG at a hardcoded
  `"\n## [0.16.1]"` to find "everything above the first dated release", so the 0.17.0 cut
  silently pushed a 682-line shipped block into the slice it polices as live text. This one
  is fail-*closed* — a spurious red, never a missed defect — which is precisely why it
  would have widened at every release until someone noticed. It is now derived with the
  same regex shape `tests/test_changelog.py` uses. Grep for a hardcoded `## [` anchor
  before you cut.

**3. Bump the version and the pinned refs.** `pyproject.toml`, then grep the tree for the
previous `@vX.Y.Z` — README, ARCHITECTURE.md and docs/modules.md carry install lines
pinned to a tag, and a stale one sends new users to the previous release.

**4. Green the gates.** Full suite and `scripts/check_arch_cards.py`. An out-of-sync
architecture-card anchor is a hard CI failure. Run `ruff check` on changed files too, but
it is advisory: CI runs it with `|| true`, so lint never fails the build (see
CONTRIBUTING.md). Also run
`scripts/acceptance.sh` — it builds the wheel, installs it fresh, and proves the
*installed* artifact rather than the checkout (techne#1059); CI's `acceptance` job already
runs it on every PR, so a green PR covers this, but re-run it by hand against the tag
after step 6 to catch anything CI's build couldn't see (e.g. a stale local cache).

**5. Merge the PR**, squash, with the PR body as the commit message.

**6. Tag, annotated, and push the tag.**

```sh
git tag -a vX.Y.Z -m "techne vX.Y.Z — <one line>"
git push origin vX.Y.Z
```

**7. Publish the GitHub Release, sourcing its body from the CHANGELOG section.**

```sh
gh release create vX.Y.Z --verify-tag --title "vX.Y.Z" --notes-file <(...)
```

The body **must** carry the `### Upgrading` text from step 2. This is the whole point of
the exercise: the Release page is what a `pipx install ... @vX.Y.Z` user reads, and it is
the only surface no test can check. If you write the body by hand and paraphrase, the
upgrade steps are exactly what gets paraphrased away — that is what happened to v0.16.0.

**8. Publish the wheel to `https://pypi.tenish.ai/simple`.** Maintainers only: publishing
needs write access to the index. Run it on the build host. Build the wheel from the tag
twice, from clean exports, with `SOURCE_DATE_EPOCH` set to the tag commit's time, and check
that the two hashes match. A mismatch means the build is not reproducible; stop and find out
why before publishing anything.

```sh
export SOURCE_DATE_EPOCH="$(git log -1 --format=%ct vX.Y.Z)"
for n in 1 2; do
  src="$(mktemp -d)"
  git archive vX.Y.Z | tar -x -C "$src"
  uv build --wheel --out-dir "dist-$n" "$src"
done
sha256sum dist-1/*.whl dist-2/*.whl   # the two hashes must be equal
```

Publish it with `publish.py` from a checkout of tenishai/tenish-pypi, dry run first. The
index is immutable: a published file cannot be replaced, so a mistake is fixed by a new
version, never a re-upload.

```sh
python3 -I publish.py --dry-run <path to>/dist-1/tenish_techne-X.Y.Z-py3-none-any.whl
python3 -I publish.py <path to>/dist-1/tenish_techne-X.Y.Z-py3-none-any.whl
```

Check that the index serves the hash you built. The project page carries it as a
`#sha256=` fragment on the file's link. While the index is in limited release it needs a
tester token:

```sh
curl -fsS -u "$UV_INDEX_TENISH_USERNAME:$UV_INDEX_TENISH_PASSWORD" \
  https://pypi.tenish.ai/simple/tenish-techne/ \
  | grep -o 'tenish_techne-X.Y.Z-py3-none-any.whl#sha256=[0-9a-f]*'
sha256sum dist-1/tenish_techne-X.Y.Z-py3-none-any.whl   # same hash
```

Then attach the wheel and a `SHA256SUMS` file to the GitHub Release:

```sh
(cd dist-1 && sha256sum tenish_techne-X.Y.Z-py3-none-any.whl > SHA256SUMS)
gh release upload vX.Y.Z dist-1/tenish_techne-X.Y.Z-py3-none-any.whl dist-1/SHA256SUMS
```

**Then verify it from the outside**, as a tag-installer would see it:

```sh
gh release view vX.Y.Z --json body --jq .body | grep -c "Upgrading"
```

Zero means the release is published and the upgrade steps are not on it. Fix it with
`gh release edit` before telling anyone the release is out.

Check that the version resolves from the index, and that the Release carries the wheel and
`SHA256SUMS`:

```sh
echo 'tenish-techne==X.Y.Z' > release.in
uv pip compile --index tenish=https://pypi.tenish.ai/simple release.in
gh release view vX.Y.Z --json assets --jq '.assets[].name'
```

The index must be named (`tenish=`): uv reads `UV_INDEX_TENISH_USERNAME` and
`UV_INDEX_TENISH_PASSWORD`, the tester token from step 8, only for a named index. A 401 means
the credentials are missing, not the wheel. Any other resolution failure means step 8 was
skipped or failed, which is what happened to v0.20.0.

**Then install the tag as a user would** (techne#1319): both README paths, from the tag
over https, through `techne init`, the hooks, MCP and a store and recall:

```sh
scripts/install-e2e.sh --ref vX.Y.Z
```

Run it on the build host, not a workstation (it reads `GH_TOKEN` while the repo is
private). CI's `install-e2e` job already ran the same steps against the PR's commit; this
run checks the tag as published.

## Repairing a release already published

Release bodies are editable and editing one is not rewriting history — the tag and the
artifacts are untouched. If a shipped release is missing its upgrade steps, add a short
block at the top pointing at the fixed version rather than leaving tag-installers to find
out from a broken install. `gh release edit vX.Y.Z --notes-file -` takes the new body on
stdin.
