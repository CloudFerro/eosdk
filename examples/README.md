# eosdk examples

Runnable, self-contained examples for the `eosdk` library and the `eo` CLI.

## Layout

| Example | Shows |
|---|---|
| [python/01_search_basic.py](python/01_search_basic.py) | Catalogue search, lazy `SearchResult` paging, collections |
| [python/02_download_products.py](python/02_download_products.py) | Bulk download, progress callbacks, `DownloadReport` |
| [python/03_list_and_open.py](python/03_list_and_open.py) | Listing files inside a product, ranged reads over S3 |
| [python/04_auth_and_s3_keys.py](python/04_auth_and_s3_keys.py) | Login flows, session status, S3 key lifecycle, boto3 interop |
| [python/05_configuration.py](python/05_configuration.py) | Profiles, endpoint overrides, env vars, discovery |
| [python/06_error_handling.py](python/06_error_handling.py) | The exception taxonomy and how to react to each error |
| [python/07_raw_queries.py](python/07_raw_queries.py) | Escape hatches: raw OData queries, raw STAC bodies, `Product.raw` |
| [python/08_plugin_backend.py](python/08_plugin_backend.py) | Extending the SDK with third-party catalogue/downloader plugins |
| [cli/01_search_and_download.sh](cli/01_search_and_download.sh) | `eo search` piped into `eo download`, JSON Lines + `jq` |
| [cli/02_auth.sh](cli/02_auth.sh) | Device flow, password login, status in scripts |
| [cli/03_s3_keys.sh](cli/03_s3_keys.sh) | Key lifecycle, `--export` for aws-cli / rclone |
| [cli/04_config_profiles.sh](cli/04_config_profiles.sh) | Profiles, per-endpoint pins, `eo config show` |
| [cli/05_discovery_and_doctor.sh](cli/05_discovery_and_doctor.sh) | Service discovery, health checks, pipeline exit codes |

## Prerequisites

```bash
pip install eosdk            # or, inside this repo: uv sync
```

Most examples talk to a real platform. Connect to one first — e.g. the
Copernicus Data Space Ecosystem, used as the example platform throughout:

```bash
eo config init --platform https://discover.dataspace.copernicus.eu
```

Anonymous catalogue search then works without an account, while downloads,
S3 keys, and auth examples need a logged-in session:

```bash
eo auth login
```

To run against a different platform, set a profile or environment variables
first (see [python/05_configuration.py](python/05_configuration.py) and
[cli/04_config_profiles.sh](cli/04_config_profiles.sh)):

```bash
export EOSDK_PLATFORM=https://platform.example.eu   # rest discovered
```

## Running

```bash
# library examples
python examples/python/01_search_basic.py
# in this repo:
uv run python examples/python/01_search_basic.py

# CLI walkthroughs (read them first — a couple of steps need a login)
bash examples/cli/01_search_and_download.sh
```
