# CLI reference

Every command accepts `--profile` and `-v` (full tracebacks). Read commands
offer JSON output for pipelines (`--format json` on `search`, `--json`
elsewhere); `eo doctor` exits non-zero on failure so it works as a
pre-flight step: `eo doctor && eo download ...`.

Every CLI verb maps to a `Client` method of the same name — `eo search`
↔ `client.search()`, `eo get` ↔ `client.get()`, `eo list` ↔ `client.list()`,
`eo cat` ↔ `client.open()`, and so on — so moving between the two surfaces is
mechanical. The only library-only surface is the lower-level raw-query escape
hatch (`ODataCatalogue.query_raw` / `StacCatalogue.raw_search`, SPEC §7.3).

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
          --protocol stac --format json
```

`--filter` values accept operator prefixes: `<`, `<=`, `>`, `>=`, `!=`, `=`
(bare value = equality).

`--format` picks the output: `table` (default), `json` (JSON Lines),
`id` (one product uuid per line), or `s3` (one S3 path per line,
for `eo download --via s3` or external S3 tooling).

## get, collections, queryables

```bash
eo get <uuid> [--protocol stac|odata] [--json]   # one product's metadata
eo collections [--protocol stac|odata] [--json]  # collection ids the catalogue offers
eo queryables <collection> [--protocol] [--json] # a collection's --filter attribute names
```

## download, list, cat

```bash
eo download <uuid> [-o DIR] [--via http|s3] [-c N] [--resume/--no-resume] [--no-checksum]
eo download s3://eodata/.../PRODUCT.SAFE --via s3
eo search ... --format json | eo download - --via s3 -c 8
eo download $(eo search ... --format id) -o ./data

eo list <uuid> [PATH] [--via http|s3] [-r] [--json]   # files inside a product
eo cat  <uuid> PATH --via s3 > out.bin                # one file, ranged read to stdout
```

`--resume` (default on) resumes partial files on the s3 backend; http always
restarts. `eo cat` and `eo list --via s3` take an S3 path (or a product whose
S3 path is known), the same reference `eo download --via s3` accepts.

## keys

```bash
eo keys create [--label X] [--fresh]   # --fresh forces a new key instead of reuse
eo keys create --export                # AWS_ACCESS_KEY_ID=... lines for aws/rclone
eo keys list [--json]
eo keys revoke <access-key> [--yes]
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
