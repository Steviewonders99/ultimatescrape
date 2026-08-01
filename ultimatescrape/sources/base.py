"""Statistical and registry data sources.

A ``DataSource`` is a declarative description of an official API: where it lives,
what auth it needs, what it covers, and what it costs you in gotchas. The
registry is deliberately *data*, not code, because the long tail of national
statistics offices is enormous and mostly follows a handful of shared protocols
(PxWeb, SDMX, OData). Describing 60 sources as data and implementing 5 protocols
as code beats writing 60 bespoke clients.

Every source records its ``gotchas``, because these APIs are uniformly strange
and the cost of rediscovering each quirk is an afternoon.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..config import api_key_for


class Auth(str, Enum):
    NONE = "none"
    KEY_FREE = "key_free"  # free registration, instant key
    KEY_PAID = "key_paid"
    OAUTH = "oauth"
    UA_REQUIRED = "ua_required"  # no key, but a descriptive User-Agent is mandatory


class Protocol(str, Enum):
    REST_JSON = "rest_json"
    SDMX = "sdmx"
    PXWEB = "pxweb"
    ODATA = "odata"
    CSV = "csv"
    HTML = "html"


@dataclass(frozen=True)
class DataSource:
    key: str
    country: str
    agency: str
    base_url: str
    protocol: Protocol
    auth: Auth
    coverage: tuple[str, ...] = ()
    env_var: str | None = None
    signup_url: str | None = None
    docs_url: str | None = None
    formats: tuple[str, ...] = ("json",)
    rate_limit: str | None = None
    gotchas: str = ""
    verified: bool = False

    @property
    def configured(self) -> bool:
        if self.auth in (Auth.NONE, Auth.UA_REQUIRED):
            return True
        return bool(self.env_var and api_key_for(self.env_var))

    @property
    def status(self) -> str:
        if self.auth in (Auth.NONE, Auth.UA_REQUIRED):
            return "ready"
        return "ready" if self.configured else f"needs {self.env_var}"

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "country": self.country,
            "agency": self.agency,
            "base_url": self.base_url,
            "protocol": self.protocol.value,
            "auth": self.auth.value,
            "coverage": list(self.coverage),
            "env_var": self.env_var,
            "signup_url": self.signup_url,
            "docs_url": self.docs_url,
            "formats": list(self.formats),
            "rate_limit": self.rate_limit,
            "gotchas": self.gotchas,
            "status": self.status,
            "url_verified": self.verified,
        }


@dataclass
class SourceResult:
    source: str
    ok: bool
    rows: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "ok": self.ok,
            "row_count": len(self.rows),
            "meta": self.meta,
            "error": self.error,
            "rows": self.rows,
        }
