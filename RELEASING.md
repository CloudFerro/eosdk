# Releasing eosdk

**Next release: 0.5.0.** The version is already bumped in the tree
(`pyproject.toml` and `src/eosdk/__init__.py` both read `0.5.0`) on the
`release/v0.5.0` branch; what is left for the cut is the changelog, the merge
to `master`, the tag, and — for the artifacts to actually land on PyPI — the
publishing setup below.

## Release history

| Version | Date | Where it exists |
| --- | --- | --- |
| v0.1.0 | 2026-07-08 | git tag only |
| v0.2.0 | 2026-07-08 | git tag only |
| v0.3.0 | 2026-07-08 | git tag only |
| v0.4.0 | 2026-07-15 | git tag only — first non-`.dev0` version |
| v0.5.0 | — | pending |

Nothing has been published to PyPI yet, so `pip install eosdk` does not work
today. The README is nonetheless already written for 0.5.0 as published —
PyPI (`uv tool install eosdk` / `pip install eosdk`) is the primary install
route there, with install-from-a-clone as the fallback — so until the upload
lands, the README is ahead of reality. Two independent reasons nothing is
published yet: Trusted Publishing is only half
wired up — the GitHub mirror exists, the PyPI side does not (below) — and
the 0.4.0 sdist was **363 MB** — `data/` (EOData
that smoke tests download) and `site/` were packaged in, well over PyPI's
100 MB per-file limit. The `[tool.hatch.build.targets.sdist]` `exclude` list
in `pyproject.toml` fixes that: the 0.5.0 sdist is ~400 KB. Do not remove
those excludes without re-checking the built size.

## Pre-1.0 checklist (from SPEC §14 / the development plan)

- [x] PyPI name `eosdk` availability — **free as of 2026-07-08**
- [x] License decision (SPEC §14.8) — **Apache-2.0** (open source, decided
      2026-07-13); `LICENSE` and `project.license` in `pyproject.toml` updated
- [ ] **Configure PyPI publishing** — route decided and the GitHub push mirror
      is **live at <https://github.com/CloudFerro/eosdk>** (2026-09-09), tags
      included. Still to do: the PyPI/TestPyPI pending publishers and the
      `pypi` environment on the mirror, plus confirming GitHub Actions is
      enabled there (see [below](#one-time-setup-publishing-route)).
      **This is the one blocker on 0.5.0 reaching PyPI**; until it is done,
      tagging `v0.5.0` produces a git tag and nothing else.
- [x] Keycloak client confirmed (SPEC §14.6): the SDK uses CDSE's public
      client **`cdse-public`** (confirmed 2026-07-13), which is already the
      SDK's built-in fallback; publish it as `auth.client_id` in the platform
      discovery document (SPEC §6.2) when the document goes live
- [x] Authenticated smoke suite run against the platform — **passed 2026-07-13**
      (`EOSDK_SMOKE=1 EOSDK_SMOKE_USERNAME=... EOSDK_SMOKE_PASSWORD=... uv run pytest tests/smoke -m smoke`);
      now tiered `smoke` / `smoke_auth` / `slow` and run nightly in CI
- [x] Benchmark run and defaults tuned — **done, results in
      [`benchmarks/RESULTS.md`](benchmarks/RESULTS.md)**; see below
- [x] Packaged sdist trimmed to a buildable source tree (see
      [release history](#release-history))
- [ ] Docs are build-verified in CI (`mkdocs build --strict`) but not deployed
      anywhere yet — no Pages job on either CI. Not a release blocker; the
      `repo_url` in `mkdocs.yml` is the only published pointer today.

### Benchmark results and tuning decision

`benchmarks/bench_download.py` was run against the live platform
(2 products, 346 MB total). Raw numbers are in
[`benchmarks/RESULTS.md`](benchmarks/RESULTS.md). They only make sense
against the platform's per-user limits for a regular account:

- **EOData via HTTP (Zipper): at most 4 concurrent sessions.** Throughput
  peaked at `concurrency=2` (8.8 MB/s) and degraded beyond that
  (c=4 → 6.8 MB/s, c=10 → 5.7 MB/s) — requests over the session limit queue
  or get rejected, so raising concurrency past 4 can only hurt.
- **EOData via S3 (Exos): up to 2 000 requests/minute.** Throughput scales
  with concurrency (c=1 → 7.3 MB/s, c=10 → 15.3 MB/s). With the default
  16 MiB parts, even c=10 issues roughly one GET per second per slot —
  two orders of magnitude below the rpm limit, so the limit is not a
  practical constraint for downloads.

Decision — **defaults kept as-is**: `concurrency=4` (at the HTTP session cap;
near-optimal for S3), `part_size=16 MiB`, `max_ranges_per_file=4`. Users
doing bulk S3 transfers can raise `--concurrency` to 8–10 for ~2× throughput;
for HTTP there is no point going above the default.

## One-time setup: publishing route

The canonical repo lives on **self-hosted GitLab**
(`gitlab.cloudferro.com`). PyPI **Trusted Publishing does not support
self-hosted GitLab instances** (only github.com, gitlab.com, Google Cloud,
ActiveState).

**Decision (2026-07-15): canonical repo on CloudFerro's GitLab, push mirror
on GitHub; publishing runs from the mirror.** Push mirroring is configured in
GitLab under *Settings → Repository → Mirroring repositories* (mirror tags
too — the release workflow triggers on tags). The existing
`.github/workflows/release.yml` then runs on tag push with no changes.

**The mirror is live: <https://github.com/CloudFerro/eosdk>** (created
2026-09-09, public, Apache-2.0 detected). It carries `master` and
`release/v0.5.0` plus the `v0.1.0`–`v0.4.0` tags, so tag mirroring works.
Note it had **no GitHub Actions runs at all** as of this writing, including
for the mirrored `master` push that `ci.yml` listens on — so before relying
on the tag-triggered publish, check that Actions is enabled for the repo and
for the `CloudFerro` org (*Settings → Actions → General*). The rc dry run in
step 6 of [Cutting a release](#cutting-a-release-step-by-step) is the cheap
way to find out. The four already-mirrored tags ran nothing and published
nothing, so there is no stale-artifact risk from them.

Then, on PyPI — **this is what is still missing**:

1. Create accounts on <https://pypi.org> **and** <https://test.pypi.org>
   (they are separate); enable 2FA on both.
2. Add a **pending publisher** for the not-yet-existing project `eosdk` on
   both PyPI and TestPyPI (*Your account → Publishing*): repository
   `CloudFerro/eosdk`, workflow `release.yml`, environment `pypi`. In the
   mirror repo, create the `pypi` environment (*Settings → Environments*),
   ideally restricted to `v*` tags.

Note that `.github/workflows/ci.yml` only runs on `master` and
`init-implementation`, so a `release/*` branch gets its CI from GitLab
(`.gitlab-ci.yml`: `lint`, `test`) plus whatever a merge request runs. The
tag-triggered `release.yml` re-runs lint, mypy and pytest on 3.10–3.14 before
publishing, so the gate is not skipped.

## Versioning

Pre-1.0, minor versions may contain breaking changes; they are called out
explicitly in `CHANGELOG.md`. Since 0.4.0 moved off the `0.1.0.dev0`
pre-release suffix, the tree carries the plain next version during
development (0.5.0 today) rather than a `.devN` suffix — so an install from
a clone reports the version it will be released as. If you ever need
in-development builds that `pip` ignores by default, use `X.Y.Z.devN`
instead.

## Cutting a release (step by step)

1. **Work on a release branch**: `release/vX.Y.Z`, cut from `master`
   (`release/v0.5.0` is the current one).
2. **Finalize the changelog**: move the `[Unreleased]` content in
   `CHANGELOG.md` under a new `## [X.Y.Z] - YYYY-MM-DD` heading, add the
   `[X.Y.Z]` compare link at the bottom and repoint `[unreleased]` at
   `vX.Y.Z...master`.
3. **Confirm the version** is `X.Y.Z` in both places and identical:
   `pyproject.toml` `version` and `src/eosdk/__init__.py` `__version__`
   (`grep -n '^version' pyproject.toml; grep -n '__version__ =' src/eosdk/__init__.py`).
4. **Sanity-check locally** — the same gates CI runs:
   ```bash
   uv run ruff format --check . && uv run ruff check . && uv run mypy
   uv run pytest                          # unit + integration + hermetic e2e
   uv run --group docs mkdocs build --strict
   uv build && uvx twine check dist/*
   ls -l dist/                            # sdist must stay well under 100 MB
   ```
   Optionally repeat the live smoke run (`EOSDK_SMOKE=1 EOSDK_SMOKE_USERNAME=...
   EOSDK_SMOKE_PASSWORD=... uv run pytest -m "smoke or smoke_auth"`). Nightly
   smoke failures alert but never block a release — an upstream outage is not a
   regression here.
5. **Merge to `master`**: commit (`git commit -am "release X.Y.Z"`), push the
   release branch and merge it into `master` via merge request on GitLab.
6. **Dry-run via TestPyPI first** (the first published release especially):
   tag the merged commit `vX.Y.Zrc1` and push it — the workflow publishes rc
   tags to TestPyPI. Verify with
   `uvx --index https://test.pypi.org/simple/ --index-strategy unsafe-best-match --from eosdk eo --help`.
7. **Final tag**: `git tag vX.Y.Z && git push origin master vX.Y.Z`, then
   confirm the GitHub mirror picked the tag up — it is the mirror's tag push
   that triggers publishing. The release workflow tests on 3.10–3.14, builds,
   `twine check`s, publishes to PyPI, and creates a GitHub release with the
   built artifacts.
8. **Verify from a clean machine**: `uvx --from eosdk eo --help` and a
   smoke search (`uvx --from eosdk eo search sentinel-2-l2a --limit 1`).
9. **Open the next cycle**: bump `master` to the next version in both files,
   start a fresh `[Unreleased]` changelog section, and bump the pinned minor
   series in the README Installation section (`uv add "eosdk~=X.Y.0"`).

## README on PyPI

`README.md` is the package's long description (`readme` in
`pyproject.toml`), so PyPI renders it as the project page. Two consequences:

- **Links must be absolute.** Relative links (`docs/`, `examples/`, …) resolve
  against pypi.org and break, so the README links to the GitHub mirror,
  `https://github.com/CloudFerro/eosdk/{tree,blob}/master/...`. In-page
  anchors such as `#installation` are fine to keep — PyPI's renderer
  (`readme_renderer`) rewrites them to its `#user-content-…` heading ids.
- **The page is frozen per release.** PyPI shows the README as it was when
  that version was uploaded; fixing it means a new release.
  `uvx twine check dist/*` (step 4) validates that it renders.

The `[project.urls]` in `pyproject.toml` (the PyPI sidebar) point at the
GitHub mirror as well, because the canonical GitLab instance is not public —
anonymous visitors are redirected to its sign-in page.
