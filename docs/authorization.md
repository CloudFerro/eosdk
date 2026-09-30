# Authorization

The SDK authenticates against a Keycloak realm and attaches the resulting JWT
to every request as a bearer token. Endpoints, realm, and client id resolve
like any other setting (see [Configuration](configuration.md)): explicit
`Client(...)` kwargs, then `EOSDK_KEYCLOAK_URL` / `EOSDK_KEYCLOAK_REALM` /
`EOSDK_KEYCLOAK_CLIENT_ID`, then local/user config, then discovery.

Login is not always required. Requests go out anonymously when no session is
cached, so public services work without logging in.
Only a service that answers `401` turns the missing session into an
`AuthError` carrying the `run eo auth login` hint.

## Logging in

The CLI verb maps 1:1 to the `client.auth` provider.

```bash
eo auth login                        # device flow when the realm advertises it
eo auth login --device               # force the device flow
eo auth login -u alice               # resource-owner password grant (prompts)
eo auth login -u alice --password-stdin   # read the password from stdin
```

`eo auth login` picks the device flow when the realm advertises a device
authorization endpoint and no `--username` was given; otherwise it falls back to
the username/password grant. Some public clients advertise the device endpoint
but have the grant disabled — the device flow then
fails with a hint to retry with `eo auth login --username <you>`.

```python
from eosdk import Client

client = Client(profile="prod")

if client.auth.supports_device_flow():
    client.auth.login_device(lambda info: print(
        f"Open {info.verification_uri_complete or info.verification_uri} "
        f"and enter {info.user_code}"
    ))
else:
    client.auth.login("alice", password)   # never hard-code; read it securely
```

## Sessions and refresh

A successful login caches the refresh and access tokens per profile under
`~/.config/eosdk/tokens/<profile>.json` (directory `0700`, file `0600`), shared
across processes so short-lived CLI invocations don't each pay a login
round-trip. The access token is refreshed transparently before it expires (with
a 30-second leeway); you only log in again once the refresh token itself
expires. On a `401` the provider forces a single refresh and retries once before
raising `AuthError`; an expired refresh token surfaces as `session expired; run
eo auth login`.

```bash
eo auth status     # profile, realm, logged-in, session-valid-until; exit 1 if not logged in
eo auth logout     # drop the cached session for the active profile
```

```python
info = client.auth.status()   # AuthStatus(profile, realm, logged_in, ...)
client.auth.logout()
```

`eo auth status` exits non-zero when there is no valid session, so it works as a
pre-flight guard: `eo auth status && eo download ...`.

## S3 backend credentials

The S3 backend never sees the Keycloak JWT. It uses separate S3 key pairs minted
by the credentials service (authenticated with the JWT). Those are managed with
`eo keys` / `client.keys` — see the [CLI reference](cli.md#keys). Tokens and S3
secrets both live under `~/.config/eosdk/` at mode `0600`, separated per profile,
and are never printed except via the explicit `eo keys create --export`.

Authentication and authorization failures all raise `AuthError`; see
[Errors](errors.md).
