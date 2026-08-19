"""Local HTTP bridge — the API surface behind the onetake guided UI.

Runs on the researcher's machine next to the venv and the run store, and is
consumed by the onetake dashboard (Market Insights → Ultimate Scrape) through
server-side proxy routes. Everything here calls the package's async Python API
directly; nothing shells out to the CLI.

    uscrape serve                    # 127.0.0.1:8791
    uscrape serve --port 9000

Security model: binds to loopback by default. CORS is restricted to known
dashboard origins, and requests carrying a JSON body therefore fail the
browser preflight from any other origin — which matters because /swarm/start
spends money. Set USCRAPE_BRIDGE_TOKEN to additionally require
`Authorization: Bearer <token>` on every request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .channels.registry import ChannelRegistry
from .config import settings
from .ga4 import reports as ga4_reports
from .ga4.client import GA4Client
from .graph.ingest import maybe_ingest
from .graph.store import KnowledgeGraph
from .jobboards import registry as jb_registry
from .jobboards.fetchers import JobBoardClient
from .llm.budget import Ledger
from .llm.client import KimiClient
from .output.export import dataset_from_swarm, export
from .projconfig import project
from .sources import registry as src_registry
from .store.report import render as render_report
from .store.run import RunStore
from .swarm.orchestrator import Swarm
from .swarm.recipes import company_research, market_research, vendor_sourcing
from .swarm.spec import SwarmSpec

log = logging.getLogger("uscrape.server")

#: Observed on a live run; the skill doc and README carry the same number.
COST_PER_AGENT_USD = 0.066

ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3001",
    "https://onetake.oneforma.com",
]

app = FastAPI(title="UltimateScrape bridge", version="0.1.0", docs_url="/api/docs")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


async def _auth(request: Request) -> None:
    token = os.getenv("USCRAPE_BRIDGE_TOKEN", "").strip()
    if not token:
        return
    supplied = request.headers.get("authorization", "")
    if supplied != f"Bearer {token}":
        raise HTTPException(401, "bad or missing bridge token")


# ---------------------------------------------------------------------------
# Live run registry


@dataclass
class LiveRun:
    run_id: str
    kind: str
    topic: str
    status: str = "running"  # running | complete | aborted | error
    stage: str = "starting"  # starting | research | merge | urls | verify | synthesis | done
    done: int = 0
    total: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    events: deque = field(default_factory=lambda: deque(maxlen=400))
    # Live ledger reference so cost keeps ticking through verification and
    # synthesis, which emit no per-unit progress events.
    ledger: Ledger | None = field(default=None, repr=False)

    def snapshot(self) -> dict:
        cost = self.ledger.cost_usd if self.ledger else self.cost_usd
        return {
            "run_id": self.run_id,
            "kind": self.kind,
            "topic": self.topic,
            "status": self.status,
            "stage": self.stage,
            "done": self.done,
            "total": self.total,
            "cost_usd": round(max(cost, self.cost_usd), 4),
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "events": list(self.events)[-40:],
        }


class RunManager:
    """Owns in-flight swarms. One process, plain dict — this is a local bridge."""

    MAX_CONCURRENT = 2

    def __init__(self) -> None:
        self.live: dict[str, LiveRun] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    @property
    def running(self) -> list[LiveRun]:
        return [r for r in self.live.values() if r.status == "running"]

    def start(
        self,
        spec: SwarmSpec,
        *,
        kind: str,
        max_cost_usd: float | None,
        synthesize: bool,
        resume_id: str | None = None,
    ) -> str:
        if len(self.running) >= self.MAX_CONCURRENT:
            raise HTTPException(409, "two runs are already in flight; wait for one to finish")
        store = RunStore.open(resume_id) if resume_id else RunStore.create(spec.topic)
        live = LiveRun(
            run_id=store.root.name,
            kind=kind,
            topic=spec.topic,
            total=len(spec.work_units()),
        )
        self.live[live.run_id] = live
        self._tasks[live.run_id] = asyncio.create_task(
            self._execute(spec, store, live, max_cost_usd, synthesize, bool(resume_id))
        )
        return live.run_id

    async def _execute(
        self,
        spec: SwarmSpec,
        store: RunStore,
        live: LiveRun,
        max_cost_usd: float | None,
        synthesize: bool,
        resume: bool,
    ) -> None:
        def progress(event: str, data: dict) -> None:
            live.events.append({"event": event, "data": data, "ts": time.time()})
            if event == "plan":
                live.stage = "research"
                live.total = data.get("units", live.total)
                live.done = live.total - data.get("todo", live.total)
            elif event == "unit":
                live.done = data.get("done", live.done)
                live.cost_usd = data.get("cost_usd", live.cost_usd)
            elif event == "merged":
                live.stage = "urls"
            elif event == "urls":
                live.stage = "verify" if spec.verifier_votes else "synthesis"
            elif event == "verified":
                live.stage = "synthesis" if synthesize else "done"
            elif event == "done":
                live.stage = "done"

        ledger = Ledger(limit_usd=max_cost_usd) if max_cost_usd else Ledger()
        live.ledger = ledger
        try:
            async with Swarm(store=store, ledger=ledger, progress=progress) as swarm:
                result = await swarm.run(spec, resume=resume, synthesize=synthesize)
            live.cost_usd = result.ledger.get("cost_usd", live.cost_usd)
            # Persist what the orchestrator does not: the rendered report,
            # synthesis text, and the configured export formats.
            try:
                store.write_report(render_report(result))
                store.write_manifest(synthesis=result.synthesis or "")
                await asyncio.to_thread(export, dataset_from_swarm(result), directory=store.root)
                ingested = await asyncio.to_thread(maybe_ingest, result)
                if ingested:
                    live.events.append({"event": "graph", "data": ingested, "ts": time.time()})
            except Exception:
                log.exception("post-run export/ingest failed for %s", live.run_id)
            live.status = "aborted" if result.stats.get("aborted") else "complete"
        except Exception as exc:
            log.exception("run %s died", live.run_id)
            live.status = "error"
            live.error = str(exc)
        finally:
            live.finished_at = time.time()
            self._tasks.pop(live.run_id, None)


manager = RunManager()


# ---------------------------------------------------------------------------
# Request models


class JobsRequest(BaseModel):
    platforms: list[str] = Field(default_factory=list)
    pay_only: bool = False
    gigs_only: bool = False


class GA4RunRequest(BaseModel):
    report: str
    days: int = 28
    property_id: str | None = None
    limit: int = 2000


class SwarmRequest(BaseModel):
    kind: Literal["company", "market", "vendor"]
    companies: list[str] = Field(default_factory=list)
    markets: list[str] = Field(default_factory=list)
    topic: str = ""
    countries: list[str] = Field(default_factory=list)
    profile: str = ""
    exclude: list[str] = Field(default_factory=list)
    verify: bool = True
    synthesize: bool = True
    max_cost_usd: float | None = None


def _build_spec(req: SwarmRequest) -> SwarmSpec:
    votes = 3 if req.verify else 0
    if req.kind == "company":
        if not req.companies:
            raise HTTPException(422, "companies is required for a company swarm")
        return company_research(req.companies, verify=votes)
    if req.kind == "market":
        if not req.markets or not req.topic.strip():
            raise HTTPException(422, "markets and topic are required for a market swarm")
        return market_research(req.markets, req.topic.strip(), verify=votes)
    if not req.countries or not req.profile.strip():
        raise HTTPException(422, "countries and profile are required for vendor sourcing")
    return vendor_sourcing(
        req.countries, req.profile.strip(), exclude=req.exclude or None, verify=votes
    )


def _estimate(spec: SwarmSpec, verify: bool) -> dict:
    agents = len(spec.work_units())
    base = agents * COST_PER_AGENT_USD
    # Verification is one call per lens per finding and dominates: it scales
    # with findings found (~8/agent × 3 lenses), not with agents. Calibrated on
    # the 2026-08-19 Appen run: 8 agents, $0.67 research, $8.85 total.
    if verify:
        low, high = base + agents * 0.4, base * 1.2 + agents * 1.6
    else:
        low, high = base, base * 1.2
    return {
        "agents": agents,
        "targets": len(spec.targets),
        "dimensions": len(spec.dimensions),
        "verify": verify,
        "est_cost_low_usd": round(low, 2),
        "est_cost_high_usd": round(high, 2),
        "ceiling_usd": settings.max_run_cost_usd,
        "cost_per_agent_usd": COST_PER_AGENT_USD,
    }


# ---------------------------------------------------------------------------
# Health / doctor


@app.get("/api/health", dependencies=[Depends(_auth)])
async def health() -> dict:
    ga4 = GA4Client()
    return {
        "ok": True,
        "service": "ultimatescrape-bridge",
        "version": app.version,
        "llm": {
            "key_set": bool(settings.api_key),
            "models": settings.models,
            "ceiling_usd": settings.max_run_cost_usd,
        },
        "ga4_configured": ga4.configured,
        "graph_enabled": project.graph.enabled,
        "runs_in_flight": len(manager.running),
    }


_doctor_cache: dict[str, Any] = {"at": 0.0, "data": None}


@app.get("/api/doctor", dependencies=[Depends(_auth)])
async def doctor(force: bool = False) -> dict:
    if not force and _doctor_cache["data"] and time.time() - _doctor_cache["at"] < 60:
        return _doctor_cache["data"]
    balance = None
    if settings.api_key:
        async with KimiClient() as llm:
            balance = await llm.check_credits()
    registry = ChannelRegistry()
    try:
        channels = await registry.doctor()
    finally:
        await registry.aclose()
    ready = {s.key for s in src_registry.ready()}
    data = {
        "llm": {
            "key_set": bool(settings.api_key),
            "models": settings.models,
            "balance": balance,
            "ceiling_usd": settings.max_run_cost_usd,
            "min_credits_usd": settings.min_credits_usd,
            "concurrency": {
                "llm": settings.llm_concurrency,
                "fetch": settings.fetch_concurrency,
            },
        },
        "channels": channels,
        "sources": {
            "total": len(src_registry.CATALOG),
            "ready": len(ready),
            "missing_keys": sorted(
                {s.env_var for s in src_registry.CATALOG if s.key not in ready and s.env_var}
            ),
        },
        "ga4_configured": GA4Client().configured,
        "checked_at": time.time(),
    }
    _doctor_cache.update(at=time.time(), data=data)
    return data


# ---------------------------------------------------------------------------
# Competitor job boards


@app.get("/api/platforms", dependencies=[Depends(_auth)])
async def platforms() -> dict:
    return {
        "platforms": [
            {
                "key": p.key,
                "company": p.company,
                "access": p.access.value,
                "ats": p.ats,
                "worker_gigs": p.worker_gigs,
                "has_pay": p.has_pay,
                "careers_url": p.careers_url,
                "notes": p.notes,
            }
            for p in jb_registry.PLATFORMS
        ]
    }


@app.post("/api/jobs", dependencies=[Depends(_auth)])
async def jobs(req: JobsRequest) -> dict:
    keys = req.platforms or [p.key for p in jb_registry.PLATFORMS if p.access.value != "browser"]
    bad = [k for k in keys if k not in jb_registry.BY_KEY]
    if bad:
        raise HTTPException(422, f"unknown platforms: {', '.join(bad)}")
    async with JobBoardClient() as client:
        listings = await client.fetch_many(keys)
    rows = [l.as_dict() for l in listings]
    if req.pay_only:
        rows = [r for r in rows if r["pay_min"] is not None or r["pay_max"] is not None]
    if req.gigs_only:
        rows = [r for r in rows if r["worker_gig"]]
    by_platform: dict[str, int] = {}
    for r in rows:
        by_platform[r["platform"]] = by_platform.get(r["platform"], 0) + 1
    return {
        "listings": rows,
        "summary": {
            "total": len(rows),
            "with_pay": sum(
                1 for r in rows if r["pay_min"] is not None or r["pay_max"] is not None
            ),
            "worker_gigs": sum(1 for r in rows if r["worker_gig"]),
            "by_platform": by_platform,
            "platforms_queried": keys,
        },
    }


# ---------------------------------------------------------------------------
# GA4


@app.get("/api/ga4/reports", dependencies=[Depends(_auth)])
async def ga4_report_catalogue() -> dict:
    return {"configured": GA4Client().configured, "reports": ga4_reports.describe()}


@app.post("/api/ga4/run", dependencies=[Depends(_auth)])
async def ga4_run(req: GA4RunRequest) -> dict:
    client = GA4Client(req.property_id)
    if not client.configured:
        raise HTTPException(409, "GA4 credential not configured on the bridge host")
    try:
        async with client:
            result = await ga4_reports.run(client, req.report, days=req.days, limit=req.limit)
    except KeyError as exc:
        raise HTTPException(422, str(exc)) from exc
    return result.as_dict()


# ---------------------------------------------------------------------------
# Knowledge graph (sqlite, sync — threadpool everything)


def _graph() -> KnowledgeGraph:
    return KnowledgeGraph()


@app.get("/api/graph/stats", dependencies=[Depends(_auth)])
async def graph_stats() -> dict:
    def go() -> dict:
        with _graph() as kg:
            return kg.stats()

    return await asyncio.to_thread(go)


@app.get("/api/graph/search", dependencies=[Depends(_auth)])
async def graph_search(q: str = Query(min_length=1), limit: int = 25) -> dict:
    def go() -> dict:
        with _graph() as kg:
            return {"results": kg.search(q, limit=limit)}

    return await asyncio.to_thread(go)


@app.get("/api/graph/entity", dependencies=[Depends(_auth)])
async def graph_entity(
    name: str, kind: str = "thing", depth: int = 1, provenance: bool = False
) -> dict:
    def go() -> dict:
        with _graph() as kg:
            entity = kg.get(name, kind)
            if not entity:
                raise HTTPException(404, f"no entity named {name!r}")
            return {
                "entity": entity,
                "edges": kg.neighbors(name, kind, depth=depth),
                "provenance": kg.provenance(name, kind) if provenance else [],
            }

    return await asyncio.to_thread(go)


# ---------------------------------------------------------------------------
# Official data sources


@app.get("/api/sources", dependencies=[Depends(_auth)])
async def sources(
    ready_only: bool = False, country: str | None = None, protocol: str | None = None
) -> dict:
    items = src_registry.CATALOG
    if ready_only:
        items = src_registry.ready()
    if country:
        items = [s for s in items if country.lower() in s.country.lower()]
    if protocol:
        items = [s for s in items if s.protocol.value == protocol]
    return {"sources": [s.as_dict() for s in items], "total": len(items)}


# ---------------------------------------------------------------------------
# Swarms


@app.post("/api/swarm/estimate", dependencies=[Depends(_auth)])
async def swarm_estimate(req: SwarmRequest) -> dict:
    return _estimate(_build_spec(req), req.verify)


@app.post("/api/swarm/start", dependencies=[Depends(_auth)])
async def swarm_start(req: SwarmRequest) -> dict:
    if not settings.api_key:
        raise HTTPException(409, "no OpenRouter key configured — run `uscrape doctor`")
    spec = _build_spec(req)
    run_id = manager.start(
        spec, kind=req.kind, max_cost_usd=req.max_cost_usd, synthesize=req.synthesize
    )
    return {"run_id": run_id, "estimate": _estimate(spec, req.verify)}


@app.post("/api/runs/{run_id}/resume", dependencies=[Depends(_auth)])
async def run_resume(run_id: str) -> dict:
    store = _open_store(run_id)
    manifest = store.read_manifest()
    try:
        spec = SwarmSpec.from_dict(manifest["spec"])
    except (KeyError, ValueError) as exc:
        raise HTTPException(422, f"run cannot be resumed: {exc}") from exc
    new_id = manager.start(
        spec,
        kind=manifest["spec"].get("kind", "custom"),
        max_cost_usd=None,
        synthesize=True,
        resume_id=run_id,
    )
    return {"run_id": new_id}


# ---------------------------------------------------------------------------
# Runs — history, detail, artifacts


def _open_store(run_id: str) -> RunStore:
    if "/" in run_id or "\\" in run_id or run_id.startswith("."):
        raise HTTPException(422, "bad run id")
    root = settings.runs_dir / run_id
    if not (root / "manifest.json").exists():
        raise HTTPException(404, f"no run {run_id!r}")
    return RunStore.open(run_id)


@app.get("/api/runs", dependencies=[Depends(_auth)])
async def runs() -> dict:
    out = []
    base = settings.runs_dir
    if base.exists():
        for d in sorted(base.iterdir(), reverse=True):
            if (d / "manifest.json").is_file():
                store = RunStore.open(d.name)
                m = store.read_manifest()
                item = {
                    "run_id": d.name,
                    "topic": m.get("topic"),
                    "status": m.get("status"),
                    "created_at": m.get("created_at"),
                    "finished_at": m.get("finished_at"),
                    "stats": m.get("stats") or {},
                    "cost_usd": (m.get("ledger") or {}).get("cost_usd"),
                }
                if live := manager.live.get(d.name):
                    item.update(status=live.status, live=live.snapshot())
                out.append(item)
    return {"runs": out}


@app.get("/api/runs/{run_id}", dependencies=[Depends(_auth)])
async def run_detail(run_id: str) -> dict:
    store = _open_store(run_id)
    m = store.read_manifest()
    live = manager.live.get(run_id)
    detail: dict[str, Any] = {
        "run_id": run_id,
        "topic": m.get("topic"),
        "status": live.status if live else m.get("status"),
        "created_at": m.get("created_at"),
        "finished_at": m.get("finished_at"),
        "stats": m.get("stats") or {},
        "ledger": m.get("ledger") or {},
        "errors": m.get("errors") or [],
        "synthesis": m.get("synthesis") or "",
        "spec_summary": {
            "topic": (m.get("spec") or {}).get("topic"),
            "targets": [t.get("label") for t in (m.get("spec") or {}).get("targets", [])],
            "dimensions": [d.get("key") for d in (m.get("spec") or {}).get("dimensions", [])],
            "verifier_votes": (m.get("spec") or {}).get("verifier_votes", 0),
        },
    }
    if live:
        detail["live"] = live.snapshot()
    elif detail["status"] == "running":
        # A run started elsewhere (CLI): progress from checkpoint files.
        planned = (m.get("spec") or {}).get("planned_units") or []
        units_dir = store.root / "units"
        done_files = list(units_dir.glob("*.json")) if units_dir.exists() else []
        cost = 0.0
        for f in done_files:
            try:
                cost += ((json.loads(f.read_text()).get("_meta") or {}).get("cost_usd")) or 0
            except Exception:  # noqa: BLE001
                pass
        detail["live"] = {
            "done": len(done_files),
            "total": len(planned),
            "cost_usd": round(cost, 4),
            "stage": "research",
            "external": True,
        }
    findings_path = store.root / "findings.json"
    if findings_path.exists():
        detail["findings"] = json.loads(findings_path.read_text())["findings"]
    files = [
        p.name
        for p in store.root.iterdir()
        if p.is_file() and p.suffix in (".md", ".json", ".csv", ".xlsx", ".html")
    ]
    detail["files"] = sorted(files)
    return detail


@app.get("/api/runs/{run_id}/report", dependencies=[Depends(_auth)])
async def run_report(run_id: str) -> PlainTextResponse:
    store = _open_store(run_id)
    for name in (f"{run_id}.md", "report.md"):
        p = store.root / name
        if p.exists():
            return PlainTextResponse(p.read_text(), media_type="text/markdown")
    raise HTTPException(404, "no report rendered for this run yet")


@app.get("/api/runs/{run_id}/files/{name}", dependencies=[Depends(_auth)])
async def run_file(run_id: str, name: str) -> FileResponse:
    store = _open_store(run_id)
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(422, "bad file name")
    p = store.root / name
    if not p.is_file() or p.suffix not in (".md", ".json", ".csv", ".xlsx", ".html"):
        raise HTTPException(404, f"no artifact {name!r}")
    return FileResponse(p, filename=name)


@app.get("/api/runs/{run_id}/export", dependencies=[Depends(_auth)])
async def run_export(run_id: str, format: str = "xlsx") -> FileResponse:
    if format not in ("xlsx", "csv", "json", "markdown", "html"):
        raise HTTPException(422, f"unsupported format {format!r}")
    store = _open_store(run_id)
    m = store.read_manifest()
    findings_path = store.root / "findings.json"
    if not findings_path.exists():
        raise HTTPException(409, "run has no findings yet")
    findings = json.loads(findings_path.read_text())["findings"]
    shim = SimpleNamespace(
        run_id=run_id,
        spec=m.get("spec") or {},
        findings=findings,
        stats=m.get("stats") or {},
        ledger=m.get("ledger") or {},
        synthesis=m.get("synthesis") or "",
        units=store.load_units(),
    )
    paths = await asyncio.to_thread(
        export, dataset_from_swarm(shim), directory=store.root, formats=[format]
    )
    real = [p for p in paths if p.suffix != ".md" or format == "markdown"]
    if not real:
        raise HTTPException(500, f"export produced nothing for {format!r}")
    return FileResponse(real[0], filename=real[0].name)


def _load_extensions() -> None:
    """Mount private extension modules listed in USCRAPE_EXTENSIONS.

    Each entry (``os.pathsep``-separated) is a path to a Python file exposing
    ``register(app)``. This keeps deployment-specific surfaces — internal data
    sources, company-specific analysis — out of this repository while letting
    them ride the same bridge process, auth, and CORS policy.
    """
    import importlib.util

    for raw in os.getenv("USCRAPE_EXTENSIONS", "").split(os.pathsep):
        raw = raw.strip()
        if not raw:
            continue
        path = Path(raw).expanduser()
        try:
            spec = importlib.util.spec_from_file_location(path.stem, path)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load {path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.register(app)
            log.info("extension mounted: %s", path)
        except Exception:
            log.exception("extension failed to load: %s", path)


_load_extensions()


def main(host: str = "127.0.0.1", port: int = 8791, reload: bool = False) -> None:
    import uvicorn

    # Relative paths in config (runs/, knowledge/graph.db, uscrape.toml discovery)
    # resolve against cwd — pin it to the repo root.
    os.chdir(Path(__file__).resolve().parent.parent)
    uvicorn.run(
        "ultimatescrape.server:app" if reload else app,
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )
