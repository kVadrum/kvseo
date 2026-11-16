"""GSC OAuth — installed-app flow + token refresh (03-connector-interfaces.md §2).

Isolated from ``gsc.py`` so the data connector stays google-auth-free and
testable. The OAuth client config comes from kvseo's managed client (env vars,
pending provisioning — R20) or a user-supplied ``client_secrets.json`` via
``--client-secrets``. Scope is ``webmasters.readonly``: read-only by design.

A refresh token is bound to the client that minted it, so ``connect gsc``
stores the client config beside the token and every later refresh uses that
stored config — not whatever the environment happens to hold that day.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

from kvseo.connectors.base import ConnectorAuthError, ConnectorUnavailable

# google-auth-oauthlib is imported lazily inside run_oauth_flow: with google-auth
# it costs ~90ms to import and is only needed for `connect gsc`, so keeping it out
# of module scope keeps every other command's startup fast. The refresh is a
# plain token-endpoint POST over httpx, so audits never import it at all.

_SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_TIMEOUT = httpx.Timeout(30.0)

# Keyring names `connect gsc` writes and every audit reads.
REFRESH_TOKEN_KEY = "gsc:refresh_token"
CLIENT_CONFIG_KEY = "gsc:client_config"


def client_config(client_secrets: Path | None = None) -> dict[str, Any]:
    """Build the OAuth client config from a file, or kvseo's managed client (env)."""
    if client_secrets is not None:
        try:
            loaded: dict[str, Any] = json.loads(client_secrets.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # A typo'd path or malformed file is a config failure, not a crash —
            # normalize to ConnectorAuthError so `connect gsc` exits 4 with a next step.
            raise ConnectorAuthError(
                f"couldn't read the OAuth client secrets at {client_secrets}: {exc}. "
                "Pass a valid Google client_secrets.json via --client-secrets."
            ) from exc
        return loaded
    client_id = os.environ.get("KVSEO_GSC_CLIENT_ID")
    client_secret = os.environ.get("KVSEO_GSC_CLIENT_SECRET")
    if not (client_id and client_secret):
        raise ConnectorAuthError(
            "No GSC OAuth client configured. kvseo's managed client is not yet "
            "provisioned (R20); set KVSEO_GSC_CLIENT_ID + KVSEO_GSC_CLIENT_SECRET, "
            "or pass --client-secrets <client_secrets.json>."
        )
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": _AUTH_URI,
            "token_uri": _TOKEN_URI,
            "redirect_uris": ["http://localhost"],
        }
    }


def run_oauth_flow(config: dict[str, Any], *, port: int = 0) -> str:
    """Run the installed-app browser flow against ``config``; return the refresh token.

    Headless/no-browser hosts are a known gap (Google retired the OOB paste
    flow): use ``--client-secrets`` on a machine with a browser, or run the
    flow there and copy the keyring entry. Tracked as an R3 follow-up.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_config(config, scopes=_SCOPES)
    creds = flow.run_local_server(port=port, prompt="consent")
    if not creds.refresh_token:
        raise ConnectorAuthError(
            "Google returned no refresh token. Revoke kvseo's access in your "
            "Google account, then re-run to force a fresh consent prompt."
        )
    return str(creds.refresh_token)


async def refresh_access_token(
    refresh_token: str, config: dict[str, Any], *, client: httpx.AsyncClient | None = None
) -> str:
    """Exchange a stored refresh token for a fresh access token.

    ``invalid_grant`` is the one answer that means the stored grant is dead
    (revoked, expired after 6 months unused, or a password change), so it is
    the one that tells the user to reconnect; a transport failure or a 5xx is
    an outage, and reconnecting would not fix it.
    """
    installed = config.get("installed") or config.get("web") or {}
    try:
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": installed["client_id"],
            "client_secret": installed["client_secret"],
        }
    except KeyError as exc:
        raise ConnectorAuthError(
            f"the stored GSC OAuth client config has no {exc} — re-run `kvseo connect gsc`"
        ) from exc
    http = client or httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        resp = await http.post(installed.get("token_uri", _TOKEN_URI), data=data)
    except httpx.HTTPError as exc:
        raise ConnectorUnavailable(f"couldn't reach Google's token endpoint: {exc}") from exc
    finally:
        if client is None:
            await http.aclose()
    if resp.status_code >= 500:
        raise ConnectorUnavailable(f"Google's token endpoint returned {resp.status_code}")
    try:
        body: dict[str, Any] = resp.json()
    except ValueError as exc:
        raise ConnectorUnavailable(f"Google's token endpoint returned non-JSON ({resp.status_code})") from exc
    if resp.status_code != 200 or "access_token" not in body:
        error = body.get("error", f"http_{resp.status_code}")
        if error == "invalid_grant":
            raise ConnectorAuthError(
                "Search Console access was revoked or has expired — re-run `kvseo connect gsc`"
            )
        raise ConnectorAuthError(f"Google refused the GSC token refresh ({error}) — re-run `kvseo connect gsc`")
    return str(body["access_token"])
