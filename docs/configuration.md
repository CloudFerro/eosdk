# Configuration

Endpoints resolve per field, most specific wins:

1. explicit kwargs to `Client(...)`
2. environment variables `EOSDK_*` (e.g. `EOSDK_EODATA_HTTP_URL`, `EOSDK_PROFILE`)
3. project-local `./eosdk.toml`
4. user config `~/.config/eosdk/config.toml` (selected profile)
5. remote discovery document (when a `platform` root is configured)
6. built-in defaults (the Copernicus Data Space Ecosystem)

`eo config show` (or `client.config.resolved()`) prints every endpoint with
the source that provided it. Endpoints that only discovery can provide show
`<pending>` until the owning service is first used.

## Profiles

```toml
default_profile = "prod"

[profiles.prod]
platform = "https://platform.example.eu"     # single root; rest discovered

[profiles.staging]
platform = "https://staging.example.eu"
eodata_http = "https://download-canary.example.eu" # pinned; pins beat discovery

[profiles.local]                              # fully manual
catalogue_stac  = "http://localhost:8081/stac"
catalogue_odata = "http://localhost:8081/odata"
eodata_http     = "http://localhost:8082"
s3_endpoint     = "http://localhost:9000"
s3_credentials  = "http://localhost:8083/api"
keycloak        = "http://localhost:8180"
keycloak_realm  = "eodata"
```

Manage from the CLI: `eo config init`, `eo config set profiles.staging.eodata_http
https://...`, `eo config use staging`, `eo config profiles`. Writes preserve
comments and layout.

`eo config init --platform <url>` fetches the platform's discovery document
immediately, names the profile after the platform's advertised name (override
with `--name`), pins every resolved endpoint, and makes it the default — so
`eo config show` reports real values, not `<pending>`. It fails if the platform
is unreachable. Re-running it resyncs a discovery-managed profile of the same
name in place (no `--force`); `--force` is only needed to overwrite a
user-owned profile that happens to share the name. Pass `--name` to keep the
discovery-managed profile separate from a hand-tuned one — the managed profile
resyncs on each `init`, while your named profile is user-owned and never
auto-touched. Omit `--platform` to enter endpoints manually instead. Run
`eo doctor` afterwards to confirm the resolved endpoints are reachable.

`discovery_url` is always derived from the `platform` root (source `derived`),
so it too resolves rather than showing `<pending>`.

## Environment variables

| Variable | Meaning |
|---|---|
| `EOSDK_PROFILE` | profile to use |
| `EOSDK_PLATFORM` | platform root for discovery |
| `EOSDK_CATALOGUE_STAC_URL`, `EOSDK_CATALOGUE_ODATA_URL` | catalogue bases |
| `EOSDK_EODATA_HTTP_URL`, `EOSDK_S3_ENDPOINT`, `EOSDK_S3_REGION` | data access |
| `EOSDK_S3_CREDENTIALS_URL` | S3 credentials service base |
| `EOSDK_KEYCLOAK_URL`, `EOSDK_KEYCLOAK_REALM`, `EOSDK_KEYCLOAK_CLIENT_ID` | auth |
| `EOSDK_DISCOVERY_URL` | explicit discovery document URL |
| `EOSDK_TLS_VERIFY` | set `0` to disable TLS verification (dev only) |

Base URLs are **version-free**: the SDK appends `/v1`-style segments itself.
`eo doctor` flags bases that end in a version segment. The one exception is
the STAC catalogue URL: it points at the self-describing STAC landing page
and is used as-is, so a version segment there is fine (CDSE's landing page
lives under `/v1`).

## Security

Tokens, S3 secrets, and cached discovery documents live under
`~/.config/eosdk/` with mode `0600`, separated per profile. Secrets are never
printed except via the explicit `eo keys create --export`.
