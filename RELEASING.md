# Releasing eosdk

## Pre-1.0 checklist (from SPEC §14 / the development plan)

- [x] PyPI name `eosdk` availability — **free as of 2026-07-08**
- [x] License decision (SPEC §14.8) — **Apache-2.0** (open source, decided
      2026-07-13); `LICENSE` and `project.license` in `pyproject.toml` updated
- [ ] Configure PyPI publishing — route decided (GitLab canonical + GitHub
      push mirror, see [below](#one-time-setup-publishing-route)); still to
      do: create the GitHub mirror and the PyPI/TestPyPI pending publishers
- [x] Keycloak client confirmed (SPEC §14.6): the SDK uses CDSE's public
      client **`cdse-public`** (confirmed 2026-07-13), which is already the
      SDK's built-in fallback; publish it as `auth.client_id` in the platform
      discovery document (SPEC §6.2) when the document goes live
- [x] Authenticated smoke suite run against the platform — **passed 2026-07-13**
      (`EOSDK_SMOKE=1 EOSDK_SMOKE_USERNAME=... EOSDK_SMOKE_PASSWORD=... uv run pytest tests/smoke -m smoke`)
- [x] Benchmark run and defaults tuned — **done, results in
      [`benchmarks/RESULTS.md`](benchmarks/RESULTS.md)**; see below

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
on GitHub; publishing runs from the mirror.** Set up a push mirror of this
repo to GitHub (GitLab: *Settings → Repository → Mirroring repositories*,
mirror tags too — the release workflow triggers on tags). The existing
`.github/workflows/release.yml` then runs on tag push with no changes.

Then, on PyPI:

1. Create accounts on <https://pypi.org> **and** <https://test.pypi.org>
   (they are separate); enable 2FA on both.
2. Add a **pending publisher** for the not-yet-existing project `eosdk` on
   both PyPI and TestPyPI (*Your account → Publishing*): repository
   `<github-owner>/eosdk` (the GitHub mirror), workflow `release.yml`,
   environment `pypi`. In the GitHub mirror repo, create the `pypi`
   environment (*Settings → Environments*), ideally restricted to `v*` tags.

## Cutting a release (step by step)

1. **Finalize the changelog**: move the `[Unreleased]` content in
   `CHANGELOG.md` under a new `## [X.Y.Z] - YYYY-MM-DD` heading and update
   the compare links at the bottom of the file.
2. **Bump the version** in two places, keeping them identical:
   `pyproject.toml` `version` and `src/eosdk/__init__.py` `__version__`
   (currently `0.1.0.dev0` — the first published release must move off the
   `.dev0` suffix; PyPI treats dev releases as pre-releases that `pip`
   ignores by default).
3. **Sanity-check locally**:
   `uv run ruff check . && uv run mypy && uv run pytest`, then
   `uv build && uvx twine check dist/*`.
4. **Commit and tag**: `git commit -am "release X.Y.Z"`, then
   `git tag vX.Y.Z && git push origin master vX.Y.Z` (with route 1, confirm
   the mirror picked the tag up).
5. **Dry-run via TestPyPI first** (first release especially): tag
   `vX.Y.Zrc1` — the workflow publishes rc tags to TestPyPI. Verify with
   `uvx --index https://test.pypi.org/simple/ --index-strategy unsafe-best-match --from eosdk eo --help`.
6. **Final tag** `vX.Y.Z`: the release workflow tests on 3.10–3.14, builds,
   `twine check`s, publishes to PyPI, and creates a GitHub release with the
   built artifacts.
7. **Verify from a clean machine**: `uvx --from eosdk eo --help` and a
   smoke search (`uvx --from eosdk eo search sentinel-2-l2a --limit 1`).
8. **Open the next cycle**: bump to `X.Y.(Z+1).dev0` (or the next minor
   `.dev0`) on `master` and start a fresh `[Unreleased]` changelog section.
