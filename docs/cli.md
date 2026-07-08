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

## download

```bash
eo download <uuid> [-o DIR] [--via zipper|exos] [-c N] [--no-checksum]
eo search ... --json | eo download - --via exos -c 8
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
eo doctor [--json]                # ✓/✗/- health checks with hints
```
