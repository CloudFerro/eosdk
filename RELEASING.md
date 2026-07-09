# Releasing eosdk

## Pre-1.0 checklist (from SPEC §14 / the development plan)

- [x] PyPI name `eosdk` availability — **free as of 2026-07-08**
- [ ] License decision (SPEC §14.8: internal vs open source) — update `LICENSE`
      and `project.license` in `pyproject.toml`
- [ ] Configure a PyPI **Trusted Publisher** for this repository
      (`.github/workflows/release.yml`, environment `pypi`)
- [ ] Confirm the dedicated Keycloak client for the SDK (SPEC §14.6) and
      publish it as `auth.client_id` in the platform discovery document
      (SPEC §6.2); the SDK's built-in fallback is CDSE's public client
      `cdse-public`
- [ ] Run the authenticated smoke suite against the platform:
      `EOSDK_SMOKE=1 EOSDK_SMOKE_USERNAME=... EOSDK_SMOKE_PASSWORD=... uv run pytest tests/smoke -m smoke`
- [ ] Run `benchmarks/bench_download.py`; record results in `benchmarks/RESULTS.md`
      and tune `part_size` / `max_ranges_per_file` / default concurrency

## Cutting a release

1. Update `CHANGELOG.md` and bump `version` in `pyproject.toml`
   (and `src/eosdk/__init__.py.__version__`).
2. `git tag vX.Y.Z && git push origin vX.Y.Z` — the release workflow tests on
   3.10–3.13, builds, publishes (TestPyPI for `-rc` tags, PyPI otherwise), and
   creates a GitHub release.
3. Verify from a clean machine: `uvx --from eosdk eo --help`.
