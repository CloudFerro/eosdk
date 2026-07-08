# Errors

All exceptions inherit `eosdk.exceptions.EosdkError`. The SDK fails early —
query translation, capability checks, and API-version guards run before any
transfer starts — and every error names the failing service and, where
possible, the config knob that fixes it.

| Exception | Raised when |
|---|---|
| `ConfigError` | bad TOML, unknown profile, missing/pending endpoint |
| `AuthError` | login/refresh failure; 401 persisting after a forced refresh |
| `EndpointUnreachable` | connect/timeout on first use or during `eo doctor` |
| `UnsupportedApiVersion` | a service advertises a version outside the supported range |
| `UnsupportedQueryFeature` | a query construct the chosen backend cannot express |
| `UnsupportedCapability` | no available strategy supports the operation (`download`/`list`/`open`) |
| `ProductNotFound` | catalogue get / download referenced an unknown product |
| `DownloadError` | transfer failed after retries (incl. checksum mismatch) |
| `QuotaExceeded` | service-side 429 (carries `retry_after` when provided) |
