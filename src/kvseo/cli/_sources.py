"""Stored credentials for the v0.2 sources, and the audit pulls built from them.

``kvseo connect <source>`` writes these keyring entries and every audit reads
them back, so the key names live here once. Each source also honours
environment variables, read directly (there is no ``env:`` indirection).
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Sized

from sqlalchemy.engine import Engine

from kvseo.config.secrets import get_secret
from kvseo.connectors.dataforseo import DataForSeoConnector
from kvseo.connectors.openseo import HOSTED_URL, OpenSeoConnector
from kvseo.connectors.serpbear import SerpBearConnector, serpbear_domain
from kvseo.core.audit.engine import SourcePull

SERPBEAR_URL = "serpbear:url"
SERPBEAR_TOKEN = "serpbear:token"
DATAFORSEO_LOGIN = "dataforseo:login"
DATAFORSEO_PASSWORD = "dataforseo:password"
DATAFORSEO_SANDBOX = "dataforseo:sandbox"
OPENSEO_URL = "openseo:url"
OPENSEO_TOKEN = "openseo:token"

Pull = Callable[[str], Awaitable[SourcePull]]


def stored(key: str, env: str) -> str | None:
    return get_secret(key) or os.environ.get(env) or None


def serpbear(engine: Engine | None = None) -> SerpBearConnector | None:
    url, token = stored(SERPBEAR_URL, "SERPBEAR_URL"), stored(SERPBEAR_TOKEN, "SERPBEAR_API_KEY")
    return SerpBearConnector(url, token, engine=engine) if url and token else None


def dataforseo(engine: Engine | None = None) -> DataForSeoConnector | None:
    login = stored(DATAFORSEO_LOGIN, "DATAFORSEO_LOGIN")
    password = stored(DATAFORSEO_PASSWORD, "DATAFORSEO_PASSWORD")
    if not (login and password):
        return None
    return DataForSeoConnector(login, password, sandbox=get_secret(DATAFORSEO_SANDBOX) == "1", engine=engine)


def openseo(engine: Engine | None = None) -> OpenSeoConnector | None:
    token = stored(OPENSEO_TOKEN, "OPENSEO_API_KEY")
    if not token:
        return None
    return OpenSeoConnector(token, url=stored(OPENSEO_URL, "OPENSEO_URL") or HOSTED_URL, engine=engine)


def audit_pulls(engine: Engine, keyword: str | None) -> dict[str, Pull]:
    """The v0.2 pulls this audit should run: only connected sources, and
    keyword metrics only when there is a target keyword to measure.

    DataForSEO and OpenSEO serve the same keyword data (OpenSEO is built on
    DataForSEO), so one audit asks only one of them — DataForSEO when both are
    connected, being the direct source and the cheaper call.
    """
    pulls: dict[str, Pull] = {}
    if (rank_tracker := serpbear(engine)) is not None:

        async def rankings(url: str) -> SourcePull:
            domain = serpbear_domain(url)
            found = await rank_tracker.keywords(domain)
            return SourcePull(source="serpbear", rows=len(found), detail=f"{len(found)} tracked keywords for {domain}")

        pulls["serpbear"] = rankings
    if keyword:
        if (dfs := dataforseo(engine)) is not None:

            async def dfs_volume(url: str) -> SourcePull:
                found = await dfs.keyword_volume([keyword])
                what = "sample search volume (sandbox)" if dfs.sandbox else "search volume"
                return SourcePull(source="dataforseo", rows=len(found), detail=_found(found, what, keyword))

            pulls["dataforseo"] = dfs_volume
        elif (oseo := openseo(engine)) is not None:

            async def oseo_volume(url: str) -> SourcePull:
                project = await oseo.project_for(url)
                found = await oseo.keyword_volume([keyword], project_id=project.id, site=url)
                return SourcePull(source="openseo", rows=len(found), detail=_found(found, "keyword metrics", keyword))

            pulls["openseo"] = oseo_volume
    return pulls


def _found(rows: Sized, what: str, keyword: str) -> str:
    # A call that answered with nothing is not the same as one that stored data.
    return f"{what} for '{keyword}'" if rows else f"no {what} returned for '{keyword}'"
