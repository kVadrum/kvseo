"""GSC OAuth — installed-app flow + token refresh (03-connector-interfaces.md §2).

Isolated from ``gsc.py`` so the data connector stays google-auth-free and
testable. The OAuth client config comes from kvseo's managed client (env vars,
pending provisioning — R20) or a user-supplied ``client_secrets.json`` via
``--client-secrets``. Scope is ``webmasters.readonly``: read-only by design.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from kvseo.connectors.base import ConnectorAuthError

_SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"


def _client_config(client_secrets: Path | None = None) -> dict[str, Any]:
    """Build the OAuth client config from a file, or kvseo's managed client (env)."""
    if client_secrets is not None:
        loaded: dict[str, Any] = json.loads(client_secrets.read_text(encoding="utf-8"))
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


def run_oauth_flow(*, client_secrets: Path | None = None, port: int = 0) -> str:
    """Run the installed-app browser flow and return the refresh token.

    Headless/no-browser hosts are a known gap (Google retired the OOB paste
    flow): use ``--client-secrets`` on a machine with a browser, or run the
    flow there and copy the keyring entry. Tracked as an R3 follow-up.
    """
    flow = InstalledAppFlow.from_client_config(_client_config(client_secrets), scopes=_SCOPES)
    creds = flow.run_local_server(port=port, prompt="consent")
    if not creds.refresh_token:
        raise ConnectorAuthError(
            "Google returned no refresh token. Revoke kvseo's access in your "
            "Google account, then re-run to force a fresh consent prompt."
        )
    return str(creds.refresh_token)


def access_token_from_refresh(
    refresh_token: str, *, client_secrets: Path | None = None
) -> str:
    """Exchange a stored refresh token for a fresh access token."""
    config = _client_config(client_secrets)
    installed = config.get("installed") or config.get("web") or {}
    creds = Credentials(  # type: ignore[no-untyped-call]  # google-auth is partially typed
        token=None,
        refresh_token=refresh_token,
        token_uri=installed.get("token_uri", _TOKEN_URI),
        client_id=installed["client_id"],
        client_secret=installed["client_secret"],
        scopes=_SCOPES,
    )
    creds.refresh(Request())  # type: ignore[no-untyped-call]
    return str(creds.token)
