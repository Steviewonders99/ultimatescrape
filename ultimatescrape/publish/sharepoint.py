"""Publish finalised documents to SharePoint via Microsoft Graph.

Authenticates as an Azure AD application using the client-credentials flow —
three environment variables, no user sign-in, no MSAL dependency:

    AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET

Two things about typical app registrations constrain this module:

* Most are granted **Sites.Selected** rather than Sites.ReadWrite.All. Under that
  permission the app can only touch sites an admin has explicitly granted, so an
  ungranted site fails with 403 no matter how correct the path is.
  :func:`preflight` checks reachability before any upload so that surfaces as a
  clear message rather than a batch that dies halfway.
* Uploading is outward-facing and irreversible from this side, so it is disabled
  by default in ``uscrape.toml`` and is always an explicit command. Finishing a
  research run never publishes anything as a side effect.

Small files go by simple PUT; anything over 4 MB uses an upload session, because
simple PUT is unreliable well below Graph's documented 250 MB ceiling.
"""

from __future__ import annotations

import logging
import time
import urllib.parse
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..config import api_key_for
from ..projconfig import project

log = logging.getLogger("uscrape.sharepoint")

GRAPH = "https://graph.microsoft.com/v1.0"
LOGIN = "https://login.microsoftonline.com"
CHUNKED_THRESHOLD = 4 * 1024 * 1024
CHUNK_SIZE = 5 * 1024 * 1024  # must be a multiple of 320 KiB per Graph's spec

CONFLICT_BEHAVIOR = {"rename": "rename", "replace": "replace", "fail": "fail"}


class SharePointError(RuntimeError):
    pass


@dataclass
class UploadResult:
    name: str
    ok: bool
    web_url: str | None = None
    size: int = 0
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "web_url": self.web_url,
            "size": self.size,
            "error": self.error,
        }


class SharePointPublisher:
    def __init__(
        self,
        *,
        site_host: str | None = None,
        site_path: str | None = None,
        folder: str | None = None,
        conflict: str | None = None,
    ) -> None:
        cfg = project.sharepoint
        self.site_host = site_host or cfg.site_host
        self.site_path = site_path or cfg.site_path
        self.folder = (folder if folder is not None else cfg.folder).strip("/")
        self.conflict = CONFLICT_BEHAVIOR.get((conflict or cfg.conflict).lower(), "rename")

        self.tenant = api_key_for("AZURE_TENANT_ID")
        self.client_id = api_key_for("AZURE_CLIENT_ID")
        self.client_secret = api_key_for("AZURE_CLIENT_SECRET")

        self._token: str | None = None
        self._token_expires: float = 0.0
        self._site_id: str | None = None
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=20.0))

    async def __aenter__(self) -> SharePointPublisher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def configured(self) -> bool:
        return bool(self.tenant and self.client_id and self.client_secret)

    # ── auth ──────────────────────────────────────────────────────────────────

    async def token(self) -> str:
        """Cached app-only token. Refreshed a minute before expiry.

        Long uploads outlive a token, and the existing scripts in the fleet cache
        without any expiry check — fine for a 10-second script, wrong for a batch.
        """
        if self._token and time.monotonic() < self._token_expires:
            return self._token
        if not self.configured:
            raise SharePointError(
                "SharePoint needs AZURE_TENANT_ID, AZURE_CLIENT_ID and AZURE_CLIENT_SECRET. "
                "Pull them from Vercel: "
                "vercel env pull .env.production   # or however your team stores them"
            )
        resp = await self._client.post(
            f"{LOGIN}/{self.tenant}/oauth2/v2.0/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            raise SharePointError(
                f"token request failed ({resp.status_code}): {resp.text[:300]}"
            )
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expires = time.monotonic() + int(payload.get("expires_in", 3600)) - 60
        return self._token

    async def _headers(self, **extra: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self.token()}", **extra}

    # ── site resolution ───────────────────────────────────────────────────────

    async def site_id(self) -> str:
        if self._site_id:
            return self._site_id
        if not (self.site_host and self.site_path):
            raise SharePointError(
                "set sharepoint.site_host and sharepoint.site_path in uscrape.toml "
                "(or SHAREPOINT_SITE_HOST / SHAREPOINT_SITE_PATH)"
            )
        path = "/" + self.site_path.strip("/")
        resp = await self._client.get(
            f"{GRAPH}/sites/{self.site_host}:{path}", headers=await self._headers()
        )
        if resp.status_code == 403:
            raise SharePointError(
                f"access denied to {self.site_host}{path}. The app registration holds "
                "Sites.Selected, so a tenant admin must grant this specific site: "
                "POST /sites/{id}/permissions with roles:[write] for client "
                f"{self.client_id[:8]}…"
            )
        if resp.status_code != 200:
            raise SharePointError(
                f"could not resolve site {self.site_host}{path} ({resp.status_code}): "
                f"{resp.text[:250]}"
            )
        self._site_id = resp.json()["id"]
        return self._site_id

    async def preflight(self) -> dict:
        """Check auth, site access and folder before uploading anything.

        Worth its own call: a failed batch halfway through leaves a partially
        published folder, which is worse than not starting.
        """
        report: dict[str, Any] = {"configured": self.configured}
        if not self.configured:
            report["error"] = "AZURE_* credentials not set"
            return report
        try:
            await self.token()
            report["token"] = "ok"
        except SharePointError as exc:
            report["error"] = str(exc)
            return report
        try:
            report["site_id"] = await self.site_id()
            report["site"] = f"{self.site_host}{self.site_path}"
        except SharePointError as exc:
            report["error"] = str(exc)
            return report

        folder = self.folder
        resp = await self._client.get(
            f"{GRAPH}/sites/{report['site_id']}/drive/root"
            + (f":/{urllib.parse.quote(folder)}:" if folder else "")
            + "/children?$top=1",
            headers=await self._headers(),
        )
        report["folder"] = folder or "(drive root)"
        report["folder_exists"] = resp.status_code == 200
        if resp.status_code not in (200, 404):
            report["error"] = f"folder check returned {resp.status_code}: {resp.text[:200]}"
        report["writable"] = "unknown until first upload"
        return report

    # ── upload ────────────────────────────────────────────────────────────────

    async def upload_file(self, path: Path, *, subfolder: str = "") -> UploadResult:
        data = path.read_bytes()
        return await self.upload_bytes(data, path.name, subfolder=subfolder)

    async def upload_bytes(
        self, content: bytes, name: str, *, subfolder: str = ""
    ) -> UploadResult:
        try:
            site = await self.site_id()
        except SharePointError as exc:
            return UploadResult(name=name, ok=False, error=str(exc))

        parts = [p for p in (self.folder, subfolder.strip("/"), name) if p]
        remote_path = "/".join(parts)
        encoded = urllib.parse.quote(remote_path)

        try:
            if len(content) <= CHUNKED_THRESHOLD:
                item = await self._simple_put(site, encoded, content)
            else:
                item = await self._session_upload(site, encoded, content, name)
        except SharePointError as exc:
            return UploadResult(name=name, ok=False, size=len(content), error=str(exc))

        return UploadResult(
            name=item.get("name", name),
            ok=True,
            web_url=item.get("webUrl"),
            size=int(item.get("size", len(content))),
        )

    async def _simple_put(self, site: str, encoded_path: str, content: bytes) -> dict:
        url = (
            f"{GRAPH}/sites/{site}/drive/root:/{encoded_path}:/content"
            f"?@microsoft.graph.conflictBehavior={self.conflict}"
        )
        resp = await self._client.put(
            url,
            content=content,
            headers=await self._headers(**{"Content-Type": "application/octet-stream"}),
        )
        if resp.status_code in (200, 201):
            return resp.json()
        if resp.status_code == 409:
            raise SharePointError(
                f"a file of that name already exists and conflict='{self.conflict}'"
            )
        raise SharePointError(f"upload failed ({resp.status_code}): {resp.text[:300]}")

    async def _session_upload(
        self, site: str, encoded_path: str, content: bytes, name: str
    ) -> dict:
        """Chunked upload for larger files.

        Graph documents simple PUT up to 250 MB, but it becomes unreliable well
        before that, so anything past 4 MB goes through a session.
        """
        create = await self._client.post(
            f"{GRAPH}/sites/{site}/drive/root:/{encoded_path}:/createUploadSession",
            json={
                "item": {
                    "@microsoft.graph.conflictBehavior": self.conflict,
                    "name": name,
                }
            },
            headers=await self._headers(**{"Content-Type": "application/json"}),
        )
        if create.status_code not in (200, 201):
            raise SharePointError(
                f"could not create upload session ({create.status_code}): {create.text[:250]}"
            )
        upload_url = create.json()["uploadUrl"]
        total = len(content)

        for start in range(0, total, CHUNK_SIZE):
            chunk = content[start : start + CHUNK_SIZE]
            end = start + len(chunk) - 1
            for attempt in range(1, 4):
                resp = await self._client.put(
                    upload_url,
                    content=chunk,
                    # The upload URL carries its own auth; sending a bearer token
                    # here makes Graph reject the chunk.
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {start}-{end}/{total}",
                    },
                )
                if resp.status_code in (200, 201, 202):
                    if resp.status_code in (200, 201):
                        return resp.json()
                    break
                if attempt == 3 or resp.status_code < 500:
                    raise SharePointError(
                        f"chunk {start}-{end} failed ({resp.status_code}): {resp.text[:200]}"
                    )
                import asyncio

                await asyncio.sleep(2**attempt)
        raise SharePointError("upload session completed without returning an item")

    async def upload_many(
        self, paths: Iterable[Path], *, subfolder: str = ""
    ) -> list[UploadResult]:
        results: list[UploadResult] = []
        for path in paths:
            result = await self.upload_file(path, subfolder=subfolder)
            results.append(result)
            log.info(
                "[sharepoint] %s %s%s",
                "uploaded" if result.ok else "FAILED",
                path.name,
                "" if result.ok else f" — {result.error}",
            )
        return results

    async def list_folder(self, subfolder: str = "") -> list[dict]:
        site = await self.site_id()
        folder = "/".join(p for p in (self.folder, subfolder.strip("/")) if p)
        url = (
            f"{GRAPH}/sites/{site}/drive/root"
            + (f":/{urllib.parse.quote(folder)}:" if folder else "")
            + "/children?$select=name,size,webUrl,lastModifiedDateTime&$top=200"
        )
        resp = await self._client.get(url, headers=await self._headers())
        if resp.status_code != 200:
            raise SharePointError(f"list failed ({resp.status_code}): {resp.text[:250]}")
        return [
            {
                "name": item.get("name"),
                "size": item.get("size"),
                "modified": item.get("lastModifiedDateTime"),
                "url": item.get("webUrl"),
            }
            for item in resp.json().get("value", [])
        ]


async def publish_paths(
    paths: Sequence[Path], *, subfolder: str = "", dry_run: bool = False
) -> list[UploadResult]:
    """Upload a set of files, honouring the project's format allow-list.

    ``dry_run`` runs preflight and reports what would be sent without sending it.
    """
    cfg = project.sharepoint
    allowed_ext = {
        "markdown": ".md", "json": ".json", "jsonl": ".jsonl", "csv": ".csv",
        "xlsx": ".xlsx", "html": ".html", "mermaid": ".mmd",
    }
    keep = {allowed_ext[f] for f in cfg.upload_formats if f in allowed_ext}
    selected = [p for p in paths if p.suffix.lower() in keep or p.name == "index.md"]

    if not selected:
        return [
            UploadResult(
                name="(nothing)",
                ok=False,
                error=f"no files matched sharepoint.upload_formats={cfg.upload_formats}",
            )
        ]

    async with SharePointPublisher() as sp:
        if dry_run:
            report = await sp.preflight()
            return [
                UploadResult(
                    name=p.name,
                    ok=not report.get("error"),
                    size=p.stat().st_size,
                    error=report.get("error"),
                    web_url=f"(dry run → {report.get('site', '?')}/{sp.folder}/{subfolder})",
                )
                for p in selected
            ]
        return await sp.upload_many(selected, subfolder=subfolder)
