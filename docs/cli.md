# CLI reference

Every command accepts `--profile` and `-v` (full tracebacks). Read commands
offer `--json` for pipelines; `eo doctor` exits non-zero on failure so it
works as a pre-flight step: `eo doctor && eo download ...`.

## auth

```bash
eo auth login                 # device flow when available, else username/password
eo auth login -u alice --password-stdin
eo auth status                # exit 1 when not logged in
eo auth logout
```

## search

```bash
eo search --collection sentinel-2-l2a \
          --bbox 22.5,52.9,24.0,53.5 \
          --from 2026-06-01 --to 2026-06-30 \
          --filter "cloudCover=<20" --filter "productType=S2MSI2A" \
          --limit 50 --sort -datetime \
          --protocol stac --json
```

`--filter` values accept operator prefixes: `<`, `<=`, `>`, `>=`, `!=`, `=`
(bare value = equality).

`--format` picks the output: `table` (default), `json` (JSON Lines, same as
`--json`), `id` (one product uuid per line), or `s3` (one S3 path per line,
for `eo download --via s3` or external S3 tooling).

## download

```bash
eo download <uuid> [-o DIR] [--via http|s3] [-c N] [--no-checksum]
eo download s3://eodata/.../PRODUCT.SAFE --via s3
eo search ... --json | eo download - --via s3 -c 8
eo download $(eo search ... --format id) -o ./data
```

## keys

```bash
eo keys create [--label X]        # secrets masked
eo keys create --export           # AWS_ACCESS_KEY_ID=... lines for aws/rclone
eo keys list [--json]
eo keys revoke <access-id> [--yes]
```

## config, discover, doctor

```bash
eo config init|show|set|use|profiles
eo discover [--refresh] [--json]  # the platform's service discovery document
eo doctor [--json] [--force]      # ✓/✗/- health checks with hints
```

Besides reachability, `eo doctor` asks the HTTP and S3 data-access services whether the eodata
store behind them is available (their `/ready` endpoints). These probes are
rate-limited: the verdict is kept on disk (`~/.config/eosdk/readiness`) and
reused for 5 minutes, so repeated doctor runs — e.g. as a CI pre-flight —
don't hammer the services. A reused verdict is marked `[cached …]` in the
check's detail; `--force` probes live regardless (and restarts the
5-minute window).
