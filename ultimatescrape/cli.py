"""Command line interface.

uscrape doctor                          which backends and data sources are live
uscrape fetch <url> [...]               fetch pages as markdown
uscrape linkedin <url> [...]            resolve LinkedIn URLs through the tier chain
uscrape company "Acme" "Globex"         company-intelligence swarm
uscrape market "United States" -t "..." market-research swarm
uscrape vendor -c US -c BR -p "..."     supplier-sourcing swarm
uscrape resume <run_id>                 finish an interrupted run
uscrape sources [--protocol pxweb]      browse the statistics-API catalog
uscrape census --vars B01003_001E       query the US Census directly
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from .config import settings

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Swarm research and scraping. Kimi K2.6 agents, web + LinkedIn + official statistics.",
)
console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )


def _progress_printer():
    def report(event: str, data: dict) -> None:
        if event == "plan":
            console.print(
                f"[bold]run {data['run_id']}[/] — {data['todo']} agents to run "
                f"({data['resumed']} resumed)"
            )
        elif event == "unit" and data["done"] % 5 == 0:
            console.print(
                f"  {data['done']}/{data['total']} agents · ${data['cost_usd']:.3f}", style="dim"
            )
        elif event in ("merged", "urls", "verified", "done"):
            console.print(f"[cyan]{event}[/] {data}")

    return report


async def _run_spec(spec, *, resume_id: str | None, verify: bool, no_synth: bool):
    from .graph.ingest import maybe_ingest
    from .output import dataset_from_swarm, export
    from .projconfig import project
    from .store.run import RunStore
    from .swarm.orchestrator import Swarm

    store = RunStore.open(resume_id) if resume_id else RunStore.create(spec.topic)
    if not verify:
        spec.verifier_votes = 0

    async with Swarm(store=store, progress=_progress_printer()) as swarm:
        result = await swarm.run(spec, resume=bool(resume_id), synthesize=not no_synth)

    paths = export(dataset_from_swarm(result), directory=store.root)
    console.print()
    console.print(f"[green]done[/] · {result.stats['findings_unique']} unique findings")
    for path in paths:
        console.print(f"  {path.suffix.lstrip('.') or 'idx':<5} {path}")
    console.print(f"  spend ${result.ledger['cost_usd']:.4f} over {result.ledger['calls']} calls")

    if counts := maybe_ingest(result):
        console.print(
            f"  graph +{counts['entities']} entities, +{counts['relations']} relations "
            f"→ {project.graph.path}",
            style="dim",
        )
    if result.errors:
        console.print(f"  [yellow]{len(result.errors)} failures — see the report[/]")
    return result


# ── diagnostics ───────────────────────────────────────────────────────────────


@app.command()
def doctor(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    """Check every backend and data source before committing to a large run."""
    _setup_logging(verbose)

    async def main() -> None:
        from .channels.registry import ChannelRegistry
        from .sources.registry import CATALOG

        console.print("\n[bold]LLM[/]")
        table = Table(show_header=True, header_style="bold")
        table.add_column("Setting")
        table.add_column("Value")
        table.add_row("base_url", settings.llm_base_url)
        table.add_row("models", " → ".join(settings.models))
        table.add_row("api key", "[green]set[/]" if settings.api_key else "[red]MISSING[/]")
        table.add_row("budget ceiling", f"${settings.max_run_cost_usd:.2f}/run")
        table.add_row(
            "concurrency", f"{settings.llm_concurrency} llm / {settings.fetch_concurrency} fetch"
        )
        console.print(table)

        if settings.api_key:
            from .llm.client import KimiClient

            async with KimiClient() as llm:
                credits = await llm.check_credits()
            if credits:
                colour = "green" if credits["remaining"] > settings.min_credits_usd else "red"
                console.print(f"  balance: [{colour}]${credits['remaining']:.2f}[/]")

        console.print("\n[bold]Channels[/]")
        registry = ChannelRegistry()
        try:
            report = await registry.doctor()
        finally:
            await registry.aclose()
        table = Table(show_header=True, header_style="bold")
        for col in ("Channel", "Backend", "Health", "Detail", "Risk"):
            table.add_column(col)
        for channel, backends in report.items():
            for backend in backends:
                colour = {"ok": "green", "degraded": "yellow"}.get(backend["health"], "dim")
                table.add_row(
                    channel,
                    backend["backend"],
                    f"[{colour}]{backend['health']}[/]",
                    backend["detail"][:60],
                    "[red]yes[/]" if backend["risk"] else "",
                )
        console.print(table)

        ready = [s for s in CATALOG if s.configured]
        keyed = [s for s in CATALOG if not s.configured]
        console.print(
            f"\n[bold]Data sources[/] — {len(ready)}/{len(CATALOG)} usable now, "
            f"{len(keyed)} awaiting a key"
        )
        if keyed:
            console.print(
                "  missing keys: " + ", ".join(sorted({s.env_var for s in keyed if s.env_var})),
                style="dim",
            )

    asyncio.run(main())


# ── fetching ──────────────────────────────────────────────────────────────────


@app.command()
def fetch(
    urls: list[str] = typer.Argument(..., help="URLs to fetch"),
    out: Path | None = typer.Option(None, "--out", "-o", help="Write JSON here"),
    browser: bool = typer.Option(False, "--browser", help="Force the headless-browser tier"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Fetch URLs and extract them to markdown."""
    _setup_logging(verbose)

    async def main() -> None:
        from .channels.registry import ChannelRegistry

        registry = ChannelRegistry()
        try:
            results = await registry.fetch_many(list(urls), force_browser=browser)
        finally:
            await registry.aclose()

        for result in results:
            mark = "[green]ok[/]" if result.ok else "[red]fail[/]"
            console.print(f"{mark} [{result.backend}] {result.url}")
            if result.error:
                console.print(f"    {result.error}", style="red")
            elif result.markdown:
                preview = result.markdown.strip().splitlines()[:3]
                for line in preview:
                    console.print(f"    {line[:110]}", style="dim")

        if out:
            out.write_text(json.dumps([r.as_dict() for r in results], indent=2, ensure_ascii=False))
            console.print(f"\nwrote {out}")

    asyncio.run(main())


@app.command()
def linkedin(
    urls: list[str] = typer.Argument(..., help="LinkedIn company or profile URLs"),
    employees: bool = typer.Option(False, "--employees", help="Also scrape the employee list"),
    out: Path | None = typer.Option(None, "--out", "-o"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Resolve LinkedIn URLs through the configured tier chain."""
    _setup_logging(verbose)

    async def main() -> None:
        from .channels.linkedin import LinkedInChannel

        channel = LinkedInChannel()
        try:
            results = await asyncio.gather(
                *(channel.fetch(u, with_employees=employees) for u in urls)
            )
        finally:
            await channel.aclose()

        for result in results:
            mark = "[green]ok[/]" if result.ok else "[red]fail[/]"
            console.print(
                f"{mark} [{result.backend}] {result.url}  (tried: {', '.join(result.attempted)})"
            )
            if result.error:
                console.print(f"    {result.error}", style="red")

        if out:
            out.write_text(json.dumps([r.as_dict() for r in results], indent=2, ensure_ascii=False))
            console.print(f"\nwrote {out}")

    asyncio.run(main())


# ── swarms ────────────────────────────────────────────────────────────────────


@app.command()
def company(
    names: list[str] = typer.Argument(..., help="Company names or domains"),
    verify: bool = typer.Option(True, "--verify/--no-verify"),
    no_synth: bool = typer.Option(False, "--no-synthesis"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Company-intelligence swarm: 8 dimensions per company."""
    _setup_logging(verbose)
    from .swarm.recipes import company_research

    asyncio.run(
        _run_spec(company_research(list(names)), resume_id=None, verify=verify, no_synth=no_synth)
    )


@app.command()
def market(
    markets: list[str] = typer.Argument(..., help="Countries or regions"),
    topic: str = typer.Option(..., "--topic", "-t", help="The market or product being researched"),
    verify: bool = typer.Option(True, "--verify/--no-verify"),
    no_synth: bool = typer.Option(False, "--no-synthesis"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Market-research swarm: 7 dimensions per market."""
    _setup_logging(verbose)
    from .swarm.recipes import market_research

    asyncio.run(
        _run_spec(
            market_research(list(markets), topic), resume_id=None, verify=verify, no_synth=no_synth
        )
    )


@app.command()
def vendor(
    country: list[str] = typer.Option(..., "--country", "-c", help="Repeatable"),
    profile: str = typer.Option(..., "--profile", "-p", help="The supplier profile to match"),
    exclude: list[str] = typer.Option([], "--exclude", "-x", help="Firms to hard-exclude"),
    verify: bool = typer.Option(True, "--verify/--no-verify"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Supplier-sourcing swarm: find small local specialists per country."""
    _setup_logging(verbose)
    from .swarm.recipes import vendor_sourcing

    asyncio.run(
        _run_spec(
            vendor_sourcing(list(country), profile, exclude=list(exclude)),
            resume_id=None,
            verify=verify,
            no_synth=False,
        )
    )


@app.command()
def resume(
    run_id: str = typer.Argument(..., help="Run id, or 'latest'"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Re-dispatch only the agents that never completed, then finish the run."""
    _setup_logging(verbose)
    from .store.run import RunStore
    from .swarm.spec import SwarmSpec

    store = RunStore.latest() if run_id == "latest" else RunStore.open(run_id)
    if store is None:
        raise typer.BadParameter("no runs found")
    manifest = store.read_manifest()
    saved = manifest.get("spec") or {}
    if not saved:
        raise typer.BadParameter(f"run {store.run_id} has no spec in its manifest")

    try:
        spec = SwarmSpec.from_dict(saved)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    pending = store.pending([spec.unit_key(t, d) for t, d in spec.work_units()])
    console.print(
        f"[bold]{store.run_id}[/] — {len(pending)} of {len(spec.work_units())} agents still to run"
    )
    if not pending:
        console.print(
            "[dim]all agents already complete; re-running merge, validation, "
            "verification and synthesis over the existing units[/]"
        )
    asyncio.run(
        _run_spec(spec, resume_id=store.run_id, verify=spec.verifier_votes > 0, no_synth=False)
    )


# ── data sources ──────────────────────────────────────────────────────────────


@app.command()
def sources(
    protocol: str | None = typer.Option(None, "--protocol", help="rest_json|sdmx|pxweb|odata"),
    country: str | None = typer.Option(None, "--country"),
    ready: bool = typer.Option(False, "--ready", help="Only sources usable right now"),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Browse the catalog of official statistics and company-registry APIs."""
    from .sources.registry import CATALOG

    rows = list(CATALOG)
    if protocol:
        rows = [s for s in rows if s.protocol.value == protocol]
    if country:
        rows = [s for s in rows if country.lower() in s.country.lower()]
    if ready:
        rows = [s for s in rows if s.configured]

    if as_json:
        console.print_json(json.dumps([s.as_dict() for s in rows]))
        return

    table = Table(show_header=True, header_style="bold")
    for col in ("Key", "Country", "Agency", "Protocol", "Auth", "Status"):
        table.add_column(col)
    for source in rows:
        colour = "green" if source.configured else "yellow"
        table.add_row(
            source.key,
            source.country,
            source.agency[:34],
            source.protocol.value,
            source.auth.value,
            f"[{colour}]{source.status}[/]",
        )
    console.print(table)
    console.print(f"\n{len(rows)} sources · {sum(1 for s in rows if s.configured)} ready")


@app.command()
def census(
    variables: list[str] = typer.Option(["B01003_001E"], "--var", help="Repeatable ACS variable"),
    dataset: str = typer.Option("2023/acs/acs5", "--dataset"),
    for_geo: str = typer.Option("state:*", "--for"),
    in_geo: str | None = typer.Option(None, "--in"),
    out: Path | None = typer.Option(None, "--out", "-o"),
) -> None:
    """Query the US Census API directly."""

    async def main() -> None:
        from .sources.clients import StatsClient

        async with StatsClient() as client:
            result = await client.us_census(
                variables, dataset=dataset, for_geo=for_geo, in_geo=in_geo
            )
        if not result.ok:
            console.print(f"[red]{result.error}[/]")
            raise typer.Exit(1)
        console.print(f"[green]{len(result.rows)} rows[/] from {dataset}")
        for row in result.rows[:15]:
            console.print("  " + json.dumps(row, ensure_ascii=False)[:150], style="dim")
        if out:
            out.write_text(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
            console.print(f"wrote {out}")

    asyncio.run(main())


if __name__ == "__main__":
    app()


# ── competitor and job-board monitoring ──────────────────────────────────────


@app.command()
def jobs(
    platform: list[str] = typer.Option(
        [], "--platform", "-p", help="Repeatable. Omit to use jobboards.watch from uscrape.toml"
    ),
    pay_only: bool = typer.Option(False, "--pay-only", help="Only listings with a rate"),
    gigs_only: bool = typer.Option(False, "--gigs", help="Only worker gigs, not corporate roles"),
    out: Path | None = typer.Option(None, "--out", "-o", help="Directory for exports"),
    formats: str | None = typer.Option(None, "--format", "-f", help="Override output formats"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Pull competitor job boards and worker-gig feeds, with pay rates where published."""
    _setup_logging(verbose)

    async def main() -> None:
        from .jobboards import PLATFORMS, JobBoardClient, worker_platforms
        from .output import dataset_from_rows, export
        from .projconfig import project

        keys = list(platform) or project.jobboards.watch or [p.key for p in PLATFORMS]
        if gigs_only:
            gig_keys = {p.key for p in worker_platforms()}
            keys = [k for k in keys if k in gig_keys]
        console.print(f"fetching {len(keys)} platforms: {', '.join(keys)}", style="dim")

        async with JobBoardClient() as client:
            listings = await client.fetch_many(keys)

        rows = [listing.as_dict() for listing in listings]
        if pay_only:
            rows = [r for r in rows if r.get("pay_min")]
        rows.sort(key=lambda r: (-(r.get("pay_min") or 0), r.get("company", "")))

        with_pay = [r for r in rows if r.get("pay_min")]
        by_company: dict[str, int] = {}
        for row in rows:
            by_company[row["company"]] = by_company.get(row["company"], 0) + 1

        table = Table(show_header=True, header_style="bold")
        for col in ("Company", "Listings", "With pay", "Median rate"):
            table.add_column(col)
        for company, count in sorted(by_company.items(), key=lambda kv: -kv[1]):
            paid = [r for r in with_pay if r["company"] == company]
            hourly = sorted(r["pay_min"] for r in paid if r.get("pay_unit") == "hour")
            median = f"{hourly[len(hourly) // 2]:.2f}/hr" if hourly else "—"
            table.add_row(company[:38], str(count), str(len(paid)), median)
        console.print(table)
        console.print(f"\n{len(rows)} listings · {len(with_pay)} with published pay")

        data = dataset_from_rows(
            "Competitor job boards and worker gigs",
            rows,
            summary={
                "platforms": len(keys),
                "listings": len(rows),
                "with_pay": len(with_pay),
                "worker_gigs": sum(1 for r in rows if r.get("worker_gig")),
            },
            kind="jobboards",
        )
        paths = export(
            data,
            directory=out or Path(project.output.directory) / "exports" / "jobboards",
            formats=[f.strip() for f in formats.split(",")] if formats else None,
            basename="jobboards",
        )
        for path in paths:
            console.print(f"  {path}", style="dim")

    asyncio.run(main())


@app.command("platforms")
def platforms_cmd(as_json: bool = typer.Option(False, "--json")) -> None:
    """List the competitor platforms and how each is reached."""
    from .jobboards import PLATFORMS

    if as_json:
        console.print_json(json.dumps([p.__dict__ for p in PLATFORMS], default=str))
        return
    table = Table(show_header=True, header_style="bold")
    for col in ("Key", "Company", "Access", "Token", "Gigs", "Pay"):
        table.add_column(col)
    for p in PLATFORMS:
        table.add_row(
            p.key,
            p.company[:34],
            f"{p.access.value}{'/' + p.ats if p.ats else ''}",
            p.token or "—",
            "[green]yes[/]" if p.worker_gigs else "",
            "[green]yes[/]" if p.has_pay else "",
        )
    console.print(table)
    console.print(
        "\n[dim]Worker gigs carry the competitively useful data — rates, geography, "
        "task types. Corporate boards mostly do not.[/]"
    )


# ── knowledge graph ──────────────────────────────────────────────────────────

graph_app = typer.Typer(no_args_is_help=True, help="Local knowledge graph over captured research.")
app.add_typer(graph_app, name="graph")


@graph_app.command("stats")
def graph_stats() -> None:
    """What the graph currently holds."""
    from .graph import KnowledgeGraph

    with KnowledgeGraph() as kg:
        stats = kg.stats()
    table = Table(show_header=False)
    table.add_column("k", style="bold")
    table.add_column("v")
    for key in ("path", "entities", "relations", "observations", "sources", "runs"):
        table.add_row(key, str(stats[key]))
    console.print(table)
    if stats["entities_by_type"]:
        console.print("\n[bold]By type[/]")
        for kind, n in list(stats["entities_by_type"].items())[:12]:
            console.print(f"  {kind:<14} {n}")
    if stats["top_predicates"]:
        console.print("\n[bold]Top relations[/]")
        for pred, n in list(stats["top_predicates"].items())[:10]:
            console.print(f"  {pred:<20} {n}")


@graph_app.command("search")
def graph_search(
    query: str = typer.Argument(..., help="Name or partial name"),
    limit: int = typer.Option(20, "--limit", "-n"),
) -> None:
    """Find entities by name or alias."""
    from .graph import KnowledgeGraph

    with KnowledgeGraph() as kg:
        results = kg.search(query, limit)
    if not results:
        console.print(f"[yellow]nothing matching {query!r}[/]")
        return
    table = Table(show_header=True, header_style="bold")
    for col in ("Name", "Type", "Mentions", "Last seen"):
        table.add_column(col)
    for entity in results:
        table.add_row(
            entity["name"][:46], entity["type"], str(entity["mentions"]), entity["last_seen"][:10]
        )
    console.print(table)


@graph_app.command("show")
def graph_show(
    name: str = typer.Argument(...),
    kind: str = typer.Option("thing", "--type", "-t"),
    depth: int = typer.Option(1, "--depth", "-d"),
    provenance: bool = typer.Option(False, "--provenance", help="Show where claims came from"),
) -> None:
    """Show an entity's relations, and optionally where its claims came from."""
    from .graph import KnowledgeGraph

    with KnowledgeGraph() as kg:
        matches = kg.search(name, 1)
        resolved_kind, resolved_name = kind, name
        if matches:
            resolved_kind, resolved_name = matches[0]["type"], matches[0]["name"]
        edges = kg.neighbors(resolved_name, resolved_kind, depth=depth)
        prov = kg.provenance(resolved_name, resolved_kind) if provenance else []

    console.print(f"[bold]{resolved_name}[/] ({resolved_kind}) — {len(edges)} relations")
    table = Table(show_header=True, header_style="bold")
    for col in ("Source", "Relation", "Target", "Seen"):
        table.add_column(col)
    for edge in edges[:60]:
        table.add_row(
            edge["source"][:34],
            edge["relation"][:22],
            edge["target"][:34],
            str(int(edge["weight"])),
        )
    console.print(table)

    if provenance and prov:
        console.print("\n[bold]Provenance[/]")
        for obs in prov[:15]:
            console.print(
                f"  {obs['captured_at'][:10]} · {obs.get('agent') or '?'} · "
                f"{(obs.get('source_url') or '')[:70]}",
                style="dim",
            )


@graph_app.command("export")
def graph_export(
    out: Path | None = typer.Option(None, "--out", "-o"),
    formats: str | None = typer.Option(None, "--format", "-f"),
    predicate: str | None = typer.Option(None, "--relation", help="Filter to one relation type"),
    limit: int = typer.Option(2000, "--limit", "-n"),
) -> None:
    """Export the graph — including a Mermaid diagram you can paste anywhere."""
    from .graph import KnowledgeGraph
    from .output import dataset_from_rows
    from .output import export as do_export
    from .projconfig import project

    with KnowledgeGraph() as kg:
        edges = kg.edges(limit=limit, predicate=predicate)
        stats = kg.stats()

    data = dataset_from_rows(
        "Knowledge graph",
        edges,
        summary={k: stats[k] for k in ("entities", "relations", "sources", "runs")},
        kind="graph",
    )
    chosen = [f.strip() for f in formats.split(",")] if formats else ["json", "csv", "mermaid"]
    paths = do_export(
        data,
        directory=out or Path(project.output.directory) / "exports" / "graph",
        formats=chosen,
        basename="graph",
    )
    for path in paths:
        console.print(f"  {path}")


@graph_app.command("ingest")
def graph_ingest(
    run_id: str = typer.Argument("latest", help="Run id, or 'latest'"),
    llm: bool = typer.Option(False, "--llm", help="Also extract relations stated in prose"),
) -> None:
    """Ingest a completed run into the graph."""
    from .graph import KnowledgeGraph, extract_relations_llm, ingest_swarm_result
    from .store.run import RunStore
    from .swarm.orchestrator import SwarmResult

    store = RunStore.latest() if run_id == "latest" else RunStore.open(run_id)
    if store is None:
        raise typer.BadParameter("no runs found")
    manifest = store.read_manifest()
    findings = (
        json.loads((store.root / "findings.json").read_text()).get("findings", [])
        if (store.root / "findings.json").exists()
        else []
    )
    result = SwarmResult(
        run_id=store.run_id,
        spec=manifest.get("spec", {}),
        findings=findings,
        units=store.load_units(),
        stats=manifest.get("stats", {}),
        ledger=manifest.get("ledger", {}),
    )

    async def main() -> None:
        with KnowledgeGraph() as kg:
            counts = ingest_swarm_result(result, kg)
            console.print(
                f"[green]ingested[/] {counts['entities']} entities, "
                f"{counts['relations']} relations, {counts['sources']} sources"
            )
            if llm:
                from .llm.client import KimiClient

                async with KimiClient() as client:
                    added = await extract_relations_llm(
                        kg, result.findings, client, run_id=store.run_id
                    )
                console.print(f"[green]+{added}[/] relations from prose")

    asyncio.run(main())


# ── GA4 ──────────────────────────────────────────────────────────────────────

ga4_app = typer.Typer(no_args_is_help=True, help="GA4 reporting for the market research team.")
app.add_typer(ga4_app, name="ga4")


@ga4_app.command("reports")
def ga4_reports() -> None:
    """List the available reports and the question each answers."""
    from .ga4 import REPORTS

    table = Table(show_header=True, header_style="bold")
    for col in ("Report", "Answers", "Dimensions"):
        table.add_column(col)
    for recipe in REPORTS.values():
        table.add_row(recipe.key, recipe.question[:58], ", ".join(recipe.dimensions)[:40])
    console.print(table)
    console.print("\n[dim]uscrape ga4 run geo --days 28 --format xlsx[/]")


@ga4_app.command("properties")
def ga4_properties() -> None:
    """List every GA4 property the credential can see."""
    from .ga4 import GA4Client, GA4Error

    async def main() -> None:
        async with GA4Client() as client:
            try:
                props = await client.list_properties()
            except GA4Error as exc:
                console.print(f"[red]{exc}[/]")
                raise typer.Exit(1) from None
        table = Table(show_header=True, header_style="bold")
        for col in ("Property ID", "Name", "Account"):
            table.add_column(col)
        for prop in props:
            table.add_row(prop["property_id"], prop["name"] or "", prop["account"] or "")
        console.print(table)

    asyncio.run(main())


@ga4_app.command("run")
def ga4_run(
    report: str = typer.Argument(..., help="Report name — see `uscrape ga4 reports`"),
    days: int = typer.Option(28, "--days", "-d"),
    property_id: str | None = typer.Option(None, "--property"),
    limit: int = typer.Option(2000, "--limit", "-n"),
    out: Path | None = typer.Option(None, "--out", "-o"),
    formats: str | None = typer.Option(None, "--format", "-f"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run a named GA4 report and export it."""
    _setup_logging(verbose)

    async def main() -> None:
        from .ga4 import GA4Client, GA4Error
        from .ga4 import get as get_recipe
        from .ga4 import run as run_report
        from .output import dataset_from_rows, export
        from .projconfig import project

        try:
            recipe = get_recipe(report)
        except KeyError as exc:
            console.print(f"[red]{exc}[/]")
            raise typer.Exit(1) from None

        async with GA4Client() as client:
            try:
                result = await run_report(
                    client, report, days=days, limit=limit, property_id=property_id
                )
            except GA4Error as exc:
                console.print(f"[red]{exc}[/]")
                raise typer.Exit(1) from None

        console.print(
            f"[bold]{recipe.title}[/] — property {result.property_id}, "
            f"{result.date_range[0]} → {result.date_range[1]}"
        )
        if result.sampled:
            console.print("[yellow]note[/] GA4 sampled this response; treat totals as estimates")
        if recipe.notes:
            console.print(f"[dim]{recipe.notes}[/]")

        table = Table(show_header=True, header_style="bold")
        columns = (result.dimensions + result.metrics)[:7]
        for col in columns:
            table.add_column(col)
        for row in result.rows[:20]:
            table.add_row(*[str(row.get(c, ""))[:26] for c in columns])
        console.print(table)
        console.print(f"{result.row_count} rows")

        data = dataset_from_rows(
            f"GA4 · {recipe.title}",
            result.rows,
            summary={
                "property": result.property_id,
                "period": f"{result.date_range[0]} → {result.date_range[1]}",
                "rows": result.row_count,
                "sampled": result.sampled,
                **{f"total_{k}": v for k, v in result.totals.items()},
            },
            columns=result.dimensions + result.metrics,
            kind="ga4",
        )
        paths = export(
            data,
            directory=out or Path(project.output.directory) / "exports" / "ga4",
            formats=[f.strip() for f in formats.split(",")] if formats else None,
            basename=f"ga4-{report}",
        )
        for path in paths:
            console.print(f"  {path}", style="dim")

    asyncio.run(main())


@ga4_app.command("fields")
def ga4_fields(
    search: str | None = typer.Option(None, "--search", "-s", help="Filter by name"),
    property_id: str | None = typer.Option(None, "--property"),
) -> None:
    """List valid dimensions and metrics for a property — no more guessing names."""
    from .ga4 import GA4Client, GA4Error

    async def main() -> None:
        async with GA4Client() as client:
            try:
                meta = await client.metadata(property_id)
            except GA4Error as exc:
                console.print(f"[red]{exc}[/]")
                raise typer.Exit(1) from None
        for label in ("dimensions", "metrics"):
            items = meta[label]
            if search:
                needle = search.lower()
                items = [
                    i
                    for i in items
                    if needle in (i["api_name"] or "").lower()
                    or needle in (i["ui_name"] or "").lower()
                ]
            console.print(f"\n[bold]{label.title()}[/] ({len(items)})")
            for item in items[:60]:
                console.print(f"  {item['api_name']:<38} {item['ui_name']}", style="dim")

    asyncio.run(main())


# ── publishing ───────────────────────────────────────────────────────────────


@app.command()
def publish(
    target: str = typer.Argument("latest", help="Run id, 'latest', or a directory path"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Check access and report, upload nothing"
    ),
    subfolder: str = typer.Option("", "--subfolder", help="Folder beneath the configured root"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Upload finalised documents to SharePoint."""
    _setup_logging(verbose)

    async def main() -> None:
        from .projconfig import project
        from .publish import SharePointPublisher, publish_paths
        from .store.run import RunStore

        directory = Path(target)
        if not directory.is_dir():
            store = RunStore.latest() if target == "latest" else RunStore.open(target)
            if store is None:
                raise typer.BadParameter("no runs found")
            directory = store.root

        cfg = project.sharepoint
        files = sorted(p for p in directory.iterdir() if p.is_file())
        if not files:
            console.print(f"[yellow]nothing to upload in {directory}[/]")
            return

        async with SharePointPublisher() as sp:
            report = await sp.preflight()
        if report.get("error"):
            console.print(f"[red]{report['error']}[/]")
            raise typer.Exit(1)

        destination = f"{report.get('site', '?')}/{cfg.folder}" + (
            f"/{subfolder}" if subfolder else ""
        )
        console.print(f"[bold]destination[/] {destination}")
        console.print(f"[bold]formats[/]     {', '.join(cfg.upload_formats)}")

        # Uploading is outward-facing and visible to colleagues, so it confirms
        # unless explicitly waived.
        if not (dry_run or yes) and not typer.confirm(
            f"Upload from {directory.name} to SharePoint?"
        ):
            console.print("cancelled")
            return

        results = await publish_paths(files, subfolder=subfolder, dry_run=dry_run)
        for result in results:
            mark = "[green]ok[/]" if result.ok else "[red]fail[/]"
            console.print(f"{mark} {result.name} {result.web_url or result.error or ''}")

    asyncio.run(main())


@app.command()
def serve(
    host: str = typer.Option(
        "127.0.0.1", help="Bind address. Keep on loopback unless you know why."
    ),
    port: int = typer.Option(8791, help="Port for the bridge API."),
    reload: bool = typer.Option(False, help="Auto-reload on code changes (development)."),
) -> None:
    """Run the local HTTP bridge that the onetake dashboard talks to."""
    try:
        from .server import main as serve_main
    except ImportError as exc:  # fastapi/uvicorn are the [server] extra
        console.print(f"[red]server dependencies missing ({exc}) — run:[/]")
        console.print('  uv pip install -e ".[server]"')
        raise typer.Exit(1) from exc
    console.print(f"[bold]UltimateScrape bridge[/] on http://{host}:{port}  (docs at /api/docs)")
    serve_main(host=host, port=port, reload=reload)
