"""Authentication and S3 key lifecycle.

What this shows
---------------
* device-flow login (the default) and username/password login
* session status and logout
* the S3 Keys Manager: create / get_or_create / list / revoke
* ephemeral keys (auto-revoked context manager) for CI jobs
* handing eosdk-managed credentials to boto3

Notes
-----
Tokens and S3 secrets are cached under ``~/.config/eosdk/`` (mode 0600,
per profile), so a login survives across processes — most programs never
call ``login`` at all and just rely on the cached session created by
``eo auth login``.
"""

from __future__ import annotations

from eosdk import Client


def device_flow_login(client: Client) -> None:
    """Interactive login without a password — prints a URL + code to visit."""

    def prompt(info) -> None:  # receives DeviceCodeInfo
        url = info.verification_uri_complete or info.verification_uri
        print(f"Open {url} and enter code {info.user_code}")

    client.auth.login_device(prompt)


def password_login(client: Client, username: str, password: str) -> None:
    """Direct grant — for services/CI where the device flow is impractical."""
    client.auth.login(username, password)


def main() -> None:
    with Client() as client:
        # -- session status -------------------------------------------------------
        status = client.auth.status()
        print(f"profile={status.profile} realm={status.realm} logged_in={status.logged_in}")

        if not status.logged_in:
            device_flow_login(client)

        # The SDK injects tokens automatically on every authenticated call;
        # grab one explicitly only when calling services outside eosdk:
        token = client.auth.access_token()
        print(f"bearer token: {token[:12]}... (auto-refreshed)")

        # -- S3 keys ---------------------------------------------------------------
        # Labeled keys are idempotent: the same label returns the same pair,
        # so pipelines can call this on every run without minting garbage.
        creds = client.keys.get_or_create("my-pipeline")
        print(f"access key: {creds.access_key} (expires {creds.expiration_date or 'never'})")

        for entry in client.keys.list():           # never returns secrets
            print(f"  key {entry.access_key}  org={entry.organization or '-'}")

        # -- boto3 interop -----------------------------------------------------------
        # Uncomment if boto3 is installed:
        #
        # import boto3
        # s3 = boto3.client(
        #     "s3",
        #     endpoint_url=client.config.endpoints.exos_endpoint,
        #     aws_access_key_id=creds.access_key,
        #     aws_secret_access_key=creds.require_secret(),
        # )
        # print(s3.list_objects_v2(Bucket="eodata", Prefix="Sentinel-2/", MaxKeys=3))

        # -- ephemeral keys: minted on enter, revoked on exit — ideal for CI ---------
        with client.keys.ephemeral() as temp:
            print(f"ephemeral key {temp.access_key} lives only inside this block")

        # Revoke an explicit key when a pipeline is decommissioned:
        # client.keys.revoke(creds.access_key)

        # Drop the cached session entirely:
        # client.auth.logout()


if __name__ == "__main__":
    main()
