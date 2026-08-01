"""Catalog of official statistics and company-registry APIs.

Data, not code. Each entry records where an API lives, what auth it needs, and
the specific quirk that will otherwise cost you an afternoon. The ``gotchas``
field is the point of the file — these APIs are individually strange in ways
their documentation does not lead with.

Base URLs were verified against live documentation or a live probe on
2026-08-01; ``verified=False`` marks the handful that could not be confirmed and
should be checked before you rely on them.

Six changes here break most catalogs written before 2026 — they are called out
in the relevant ``gotchas``:

  * US Census now requires a key for *all* queries; the old 500/day keyless tier
    is gone.
  * The IMF's ``dataservices.imf.org`` SDMX-JSON API was retired 5 Nov 2025.
  * FAOSTAT moved to JWT-only auth in April 2026; its old hosts return 401/521.
  * SSB's "ready-made datasets" JSON API shut down 31 Dec 2025.
  * ``odata4.cbs.nl`` no longer resolves; CBS v4 lives at ``datasets.cbs.nl``.
  * ``api.data.abs.gov.au`` no longer resolves; the host order is now
    ``data.api.abs.gov.au``.
"""

from __future__ import annotations

from .base import Auth, DataSource, Protocol

_S = DataSource

# ── North America ─────────────────────────────────────────────────────────────

NORTH_AMERICA = [
    _S(
        key="us_census",
        country="United States",
        agency="US Census Bureau",
        base_url="https://api.census.gov/data",
        protocol=Protocol.REST_JSON,
        auth=Auth.KEY_FREE,
        env_var="CENSUS_API_KEY",
        signup_url="https://api.census.gov/data/key_signup.html",
        docs_url="https://www.census.gov/data/developers/data-sets.html",
        coverage=("population", "income", "housing", "language", "business", "poverty"),
        rate_limit="no published daily cap with a key; max 50 variables per query",
        gotchas=(
            "A key is now required for EVERY query — the often-cited 500/day keyless "
            "tier is obsolete and a keyless call 302-redirects to a 'Missing Key' page. "
            "Responses are a 2-D array whose first row is the header and where every "
            "value, including numbers, is a string. Suppressed estimates come back as "
            "sentinels like -666666666 and will wreck any aggregate. Dataset paths are "
            "vintage-scoped (/data/2023/acs/acs5) and variable codes move between "
            "vintages — discover via /data.json and read the year's variables.json."
        ),
        verified=True,
    ),
    _S(
        key="census_reporter",
        country="United States",
        agency="Census Reporter (independent)",
        base_url="https://api.censusreporter.org/1.0",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        coverage=("population", "income", "housing", "language"),
        gotchas=(
            "Keyless, which makes it the right fallback when the official API "
            "throttles. Geo IDs are prefixed: 16000US<state><place> for places, "
            "05000US<state><county> for counties, 31000US<cbsa> for metros."
        ),
        verified=True,
    ),
    _S(
        key="bls",
        country="United States",
        agency="Bureau of Labor Statistics",
        base_url="https://api.bls.gov/publicAPI/v2/timeseries/data",
        protocol=Protocol.REST_JSON,
        auth=Auth.KEY_FREE,
        env_var="BLS_API_KEY",
        signup_url="https://data.bls.gov/registrationEngine/",
        coverage=("employment", "wages", "prices", "productivity", "job openings"),
        rate_limit="registered: 500 queries/day, 50 series/query, 20 years/query; 50 req/10s",
        gotchas=(
            "Multi-series requests are POST with a JSON body; the GET form is v1 and "
            "handles one series. Errors return HTTP 200 with REQUEST_NOT_PROCESSED in "
            "the body. Series IDs are constructed codes you build yourself "
            "(CUUR0000SA0 = CPI-U all items), not names. Registration expires and "
            "needs renewing."
        ),
        verified=True,
    ),
    _S(
        key="bea",
        country="United States",
        agency="Bureau of Economic Analysis",
        base_url="https://apps.bea.gov/api/data",
        protocol=Protocol.REST_JSON,
        auth=Auth.KEY_FREE,
        env_var="BEA_API_KEY",
        signup_url="https://apps.bea.gov/api/signup/",
        coverage=("gdp", "income", "trade", "industry", "regional"),
        rate_limit="100 requests/min, 100 MB/min, 30 errors/min → HTTP 429 with RETRYAFTER",
        gotchas=(
            "Single-endpoint RPC style: every call carries &method= and &datasetname=. "
            "A keyless request returns HTTP 200 with an empty body rather than an "
            "error. Discover table names through GetParameterValues."
        ),
        verified=True,
    ),
    _S(
        key="fred",
        country="United States",
        agency="Federal Reserve Bank of St. Louis (FRED)",
        base_url="https://api.stlouisfed.org/fred",
        protocol=Protocol.REST_JSON,
        auth=Auth.KEY_FREE,
        env_var="FRED_API_KEY",
        signup_url="https://fredaccount.stlouisfed.org/apikeys",
        coverage=("macro", "rates", "employment", "prices", "money supply"),
        rate_limit="unpublished; ~120/min per key observed in practice",
        gotchas=(
            "Returns XML unless you pass file_type=json. ~800K series. ALFRED "
            "archival vintages via realtime_start/realtime_end. FRED republishes "
            "other agencies' data, so original-source licensing still applies."
        ),
        verified=True,
    ),
    _S(
        key="usaspending",
        country="United States",
        agency="USAspending.gov",
        base_url="https://api.usaspending.gov/api/v2",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        coverage=("federal contracts", "grants", "loans", "recipients", "agency budgets"),
        gotchas=(
            "Most search endpoints are POST with JSON filter payloads, not GET. Use "
            "/bulk_download for large extracts. Agency reporting lag means recent "
            "months are incomplete. V1 endpoints are deprecated."
        ),
        verified=True,
    ),
    _S(
        key="sec_edgar",
        country="United States",
        agency="SEC EDGAR",
        base_url="https://data.sec.gov",
        protocol=Protocol.REST_JSON,
        auth=Auth.UA_REQUIRED,
        docs_url="https://www.sec.gov/search-filings/edgar-application-programming-interfaces",
        coverage=("filings", "financials", "ownership", "company registry"),
        rate_limit="10 requests/second, enforced",
        gotchas=(
            "A declared User-Agent carrying a contact email is mandatory — undeclared "
            "requests get 403. CIK must be zero-padded to 10 digits. companyfacts "
            "returns every XBRL fact in one call. Full-text search (2001-present) is a "
            "separate host: efts.sec.gov/LATEST/search-index. No CORS on data.sec.gov."
        ),
        verified=True,
    ),
    _S(
        key="statcan",
        country="Canada",
        agency="Statistics Canada (Web Data Service)",
        base_url="https://www150.statcan.gc.ca/t1/wds/rest",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        docs_url="https://www.statcan.gc.ca/en/developers/wds",
        coverage=("population", "labour", "prices", "trade", "health", "agriculture"),
        rate_limit="50 req/s server-wide, 25 req/s per IP",
        gotchas=(
            "Three parallel ID systems: vector IDs (V+digits, one per series), 10-digit "
            "Product IDs for cubes, and dot-separated coordinates padded to 10 "
            "dimensions (1.12.0.0.0.0.0.0.0.0). Most data methods are POST with a "
            "JSON-array body. Responses wrap in {status, object}. CANSIM numbers are "
            "deprecated in favour of PIDs. Expect HTTP 409 during the midnight–08:30 "
            "ET maintenance window."
        ),
        verified=True,
    ),
    _S(
        key="canada_corporations",
        country="Canada",
        agency="Corporations Canada (ISED)",
        base_url="https://www.ic.gc.ca/app/scr/cc/CorporationsCanada/api/corporations",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        coverage=("company registry", "directors", "annual filings"),
        gotchas=(
            "Lookup-only — there is no search-by-name. The response is a two-element "
            "array [English|null, French|null], which breaks naive parsers. Covers "
            "FEDERAL corporations only; most Canadian companies are provincially "
            "registered with no unified national API. Director data needs the ISED "
            "API Store (free account, 60 hits/min)."
        ),
        verified=True,
    ),
    _S(
        key="inegi",
        country="Mexico",
        agency="INEGI",
        base_url="https://www.inegi.org.mx/app/api/indicadores/desarrolladores/jsonxml",
        protocol=Protocol.REST_JSON,
        auth=Auth.KEY_FREE,
        env_var="INEGI_API_TOKEN",
        signup_url="https://www.inegi.org.mx/app/desarrolladores/generatoken/Usuarios/token_Verify",
        coverage=("population", "economy", "employment", "prices"),
        gotchas=(
            "The token is a positional PATH segment, not a header or query param. "
            "BIE (economic) and BISE (everything else) are separate banks selected by "
            "path segment, and indicator IDs differ between them."
        ),
        verified=True,
    ),
]

# ── Europe ────────────────────────────────────────────────────────────────────

EUROPE = [
    _S(
        key="eurostat",
        country="European Union",
        agency="Eurostat",
        base_url="https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        docs_url="https://ec.europa.eu/eurostat/web/user-guides/data-browser/api-data-access/",
        coverage=("population", "labour", "economy", "trade", "environment", "health"),
        formats=("json-stat", "sdmx", "csv", "tsv"),
        gotchas=(
            "The single highest-leverage European source: one endpoint instead of 27 "
            "national APIs. Filters are ordinary named query parameters. Output is "
            "JSON-stat 2.0 — a dense index/value format, not readable raw. Oversized "
            "requests return HTTP 413 ASYNCHRONOUS_RESPONSE, which means re-poll the "
            "same URL rather than expecting a callback. The old /eurostat/wdds/rest "
            "services are retired. Refreshed twice daily, 11:00 and 23:00 CET."
        ),
        verified=True,
    ),
    _S(
        key="data_europa",
        country="European Union",
        agency="data.europa.eu (EU Open Data Portal)",
        base_url="https://data.europa.eu/api/hub/search",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        coverage=("dataset discovery", "metadata"),
        gotchas=(
            "A discovery API, not an observations API — it returns DCAT-AP metadata "
            "and distribution links across ~1.9M datasets, and you then fetch from the "
            "source portal. SPARQL endpoint at data.europa.eu/sparql."
        ),
        verified=True,
    ),
    _S(
        key="ons_uk",
        country="United Kingdom",
        agency="Office for National Statistics",
        base_url="https://api.beta.ons.gov.uk/v1",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        docs_url="https://developer.ons.gov.uk/",
        coverage=("population", "census 2021", "labour", "prices"),
        rate_limit="120 req/10s and 200 req/min; 15 req/10s on high-demand assets",
        gotchas=(
            "Still on a 'beta' hostname but this IS production, with a standing "
            "breaking-changes warning. Covers only a curated 'Customise My Data' "
            "subset, not the full ONS time-series universe — for detailed labour and "
            "census cross-tabs use Nomis. The old v0 API retired 25 Nov 2024. Bots "
            "must send a descriptive User-Agent, and ignoring Retry-After can earn an "
            "hour-long block."
        ),
        verified=True,
    ),
    _S(
        key="nomis_uk",
        country="United Kingdom",
        agency="Nomis (ONS labour market and census)",
        base_url="https://www.nomisweb.co.uk/api/v01",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        coverage=("labour", "census 1921-2021", "benefits", "population", "business counts"),
        formats=("json", "json-stat", "sdmx", "csv", "xlsx"),
        rate_limit="25,000 cells per download; concurrent-request cap unpublished",
        gotchas=(
            "Anonymous access works. Dataset ids look like NM_1_1 and geography codes "
            "are Nomis-internal rather than ONS codes. Discover via "
            "/dataset/def.sdmx.json. Chunk large extracts below the cell limit."
        ),
        verified=True,
    ),
    _S(
        key="companies_house",
        country="United Kingdom",
        agency="Companies House",
        base_url="https://api.company-information.service.gov.uk",
        protocol=Protocol.REST_JSON,
        auth=Auth.KEY_FREE,
        env_var="COMPANIES_HOUSE_API_KEY",
        signup_url="https://developer.company-information.service.gov.uk/manage-applications",
        coverage=("company registry", "officers", "beneficial ownership", "filings", "charges"),
        rate_limit="600 requests per 5-minute window",
        gotchas=(
            "HTTP Basic auth with the key as the USERNAME and an empty password — not "
            "a Bearer token. This is the single most common first-integration failure. "
            "The best-designed registry API anywhere: it includes PSC beneficial "
            "ownership and a separate streaming API (stream.companieshouse.gov.uk, "
            "9 change-data-capture streams) which needs its own key type. Financial "
            "line items are not exposed — accounts are filed documents."
        ),
        verified=True,
    ),
    _S(
        key="destatis",
        country="Germany",
        agency="Destatis GENESIS-Online",
        base_url="https://genesis.destatis.de/genesisWS/rest/2020",
        protocol=Protocol.REST_JSON,
        auth=Auth.KEY_FREE,
        env_var="DESTATIS_API_TOKEN",
        signup_url="https://genesis.destatis.de/datenbank/online/",
        coverage=("population", "labour", "economy", "prices", "business", "census 2022"),
        rate_limit="unpublished; limited parallel requests, self-kill after 15 min",
        gotchas=(
            "The SOAP/XML interface was shut down in H1 2025; REST/JSON replaces it. "
            "The www-genesis.destatis.de host now 307-redirects here. Credentials go "
            "in HTTP headers on POST; the older GET-with-credentials form is gone. A "
            "32-char API token substitutes for the username but does NOT work for "
            "job=true requests — large tables run as background jobs and those need "
            "username plus password. Same API serves regionalstatistik.de and the "
            "Länder instances."
        ),
        verified=True,
    ),
    _S(
        key="insee_melodi",
        country="France",
        agency="INSEE Melodi",
        base_url="https://api.insee.fr/melodi",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        coverage=("population", "economy", "labour", "local data"),
        rate_limit="30 calls/min anonymous; higher with a portal key",
        gotchas=(
            "Works anonymously, which makes it the easiest French entry point. Melodi "
            "is the designated replacement for both BDM and the local-data APIs. Its "
            "harmonised metadata can differ from the labels on insee.fr."
        ),
        verified=True,
    ),
    _S(
        key="insee_sirene",
        country="France",
        agency="INSEE Sirene (business register)",
        base_url="https://api.insee.fr/api-sirene/3.11",
        protocol=Protocol.REST_JSON,
        auth=Auth.KEY_FREE,
        env_var="INSEE_API_KEY",
        signup_url="https://portail-api.insee.fr/",
        coverage=("company registry", "establishments", "sector codes"),
        rate_limit="30 requests/minute on the free plan",
        gotchas=(
            "The old api.insee.fr WSO2 portal closed 10 Sep 2025 — any pre-2024 OAuth "
            "client-credentials integration is dead. The current auth is an "
            "X-INSEE-Api-Key-Integration header. Covers legal units and establishments "
            "but no officers and no financials (those are RNE/INPI). Non-diffusible "
            "companies return partial records. The version path segment moves."
        ),
        verified=True,
    ),
    _S(
        key="france_recherche_entreprises",
        country="France",
        agency="Recherche d'entreprises (DINUM/Etalab)",
        base_url="https://recherche-entreprises.api.gouv.fr",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        coverage=("company search", "directors", "addresses"),
        rate_limit="7 req/s per IP, plus 30 req/s per ASN",
        gotchas=(
            "Zero auth and instant — the right first stop for French company lookup, "
            "with Sirene added later for establishment depth. Search-only, and it "
            "excludes non-diffusible companies. The per-ASN limit bites on public "
            "cloud IPs even when your own rate is low."
        ),
        verified=True,
    ),
    _S(
        key="cbs_netherlands",
        country="Netherlands",
        agency="CBS StatLine",
        base_url="https://datasets.cbs.nl/odata/v1/CBS",
        protocol=Protocol.ODATA,
        auth=Auth.NONE,
        coverage=("population", "labour", "economy", "housing", "neighbourhood-level"),
        rate_limit="100,000 cells per request; page via @odata.nextLink",
        gotchas=(
            "odata4.cbs.nl is dead — this is the current v4 host. The v3 API "
            "(opendata.cbs.nl/ODataApi/odata) still works and is capped at 10,000 "
            "cells, while its Feed variant (/ODataFeed/odata) has no cap and is the "
            "sanctioned bulk route. v4 restructures to one flat Observations "
            "cell-per-row set instead of v3's wide TypedDataSet. CBS is migrating to "
            "SDMX and has said the OData channels will eventually be discontinued."
        ),
        verified=True,
    ),
    _S(
        key="scb_sweden",
        country="Sweden",
        agency="Statistics Sweden (SCB)",
        base_url="https://statistikdatabasen.scb.se/api/v2",
        protocol=Protocol.PXWEB,
        auth=Auth.NONE,
        coverage=("population", "labour", "economy", "housing"),
        rate_limit="30 calls/10s per IP; 150,000 cells per query",
        gotchas=(
            "PxWebApi 2.0, primary since Oct 2025: stable table-ID URLs (/tables, "
            "/tables/{id}/metadata, /tables/{id}/data), GET works as well as POST, and "
            "lang is a query parameter rather than a path segment. The v1 endpoint "
            "(api.scb.se/OV0104/v1/doris/en/ssd) is still live with lower limits. "
            "Read /api/v2/config for the live limits rather than trusting docs."
        ),
        verified=True,
    ),
    _S(
        key="ssb_norway",
        country="Norway",
        agency="Statistics Norway (SSB)",
        base_url="https://data.ssb.no/api/pxwebapi/v2",
        protocol=Protocol.PXWEB,
        auth=Auth.NONE,
        coverage=("population", "labour", "economy", "prices"),
        rate_limit="30 queries/60s per IP; 800,000 cells per extraction",
        gotchas=(
            "PxWebApi 2.0, primary since 29 Jan 2026. The 'ready-made datasets' API "
            "(data.ssb.no/api/v0/dataset) SHUT DOWN 31 Dec 2025, and json-stat v1 "
            "output no longer exists — v2 emits json-stat2 only. The v0 endpoint "
            "survives for a transition period with no announced end. GET URLs cap out "
            "near 2,100 chars, so use POST for large selections. Tables go briefly "
            "unavailable at 05:00 and 11:30 local."
        ),
        verified=True,
    ),
    _S(
        key="dst_denmark",
        country="Denmark",
        agency="Statistics Denmark (DST)",
        base_url="https://api.statbank.dk/v1",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        coverage=("population", "labour", "economy", "prices"),
        rate_limit="no call-rate limit published; 1,000,000 cells per non-streaming query",
        gotchas=(
            "Not PxWeb despite the Nordic grouping — DST's own design, with verbs "
            "/subjects, /tables, /tableinfo, /data. Uniquely supports server-side "
            "calculations (sum, percent, per-mille) and nth-period selection. The "
            "BULK, SDMXCOMPACT and SDMXGENERIC formats stream with no cell cap. "
            "TLS 1.2 or better is required."
        ),
        verified=True,
    ),
    _S(
        key="statfin",
        country="Finland",
        agency="Statistics Finland (Tilastokeskus)",
        base_url="https://pxdata.stat.fi/PxWeb/api/v1/en/StatFin",
        protocol=Protocol.PXWEB,
        auth=Auth.NONE,
        coverage=("population", "labour", "economy", "housing"),
        rate_limit="40 calls/60s, 120,000 cells (read ?config — the prose docs are stale)",
        gotchas=(
            "Still PxWeb 1.x, so data retrieval is POST-only. The host moved from "
            "pxnet2.stat.fi to pxdata.stat.fi and old examples 404. Table names must "
            "end in .px, unlike SCB v1. 403 means you exceeded the cell limit, 429 the "
            "rate limit, 503 a 60s timeout."
        ),
        verified=True,
    ),
    _S(
        key="cso_ireland",
        country="Ireland",
        agency="CSO PxStat",
        base_url="https://ws.cso.ie/public/api.restful",
        protocol=Protocol.PXWEB,
        auth=Auth.NONE,
        coverage=("population", "labour", "economy", "housing"),
        formats=("json-stat", "csv", "px", "xlsx"),
        gotchas=(
            "PxStat is CSO's own JSON-RPC system with a REST wrapper, NOT PxWeb — the "
            "verb is part of the path (PxStat.Data.Cube_API.ReadDataset/{matrix}/"
            "JSON-stat/2.0/en) and the language is the LAST path segment. Docs live on "
            "the CSOIreland/PxStat GitHub wiki rather than cso.ie."
        ),
        verified=True,
    ),
    _S(
        key="bfs_switzerland",
        country="Switzerland",
        agency="Federal Statistical Office (BFS/FSO)",
        base_url="https://www.pxweb.bfs.admin.ch/api/v1/en",
        protocol=Protocol.PXWEB,
        auth=Auth.NONE,
        coverage=("population", "labour", "economy"),
        rate_limit="50 calls/15s; 5,000 values / 100,000 cells (per live ?config)",
        gotchas=(
            "Classic PxWeb 1.x. Many FSO datasets are files only and never appear in "
            "STAT-TAB. Find a cube's exact API URL through the table's 'API query for "
            "this table' link. opendata.swiss (CKAN) covers the rest."
        ),
        verified=True,
    ),
    _S(
        key="istat",
        country="Italy",
        agency="ISTAT",
        base_url="https://esploradati.istat.it/SDMXWS/rest",
        protocol=Protocol.SDMX,
        auth=Auth.NONE,
        coverage=("population", "labour", "economy"),
        rate_limit="5 queries per minute per IP — breaching it blocks your IP for 1-2 DAYS",
        gotchas=(
            "By far the harshest rate limit in this catalog; build a hard client-side "
            "throttle before your first call. The old sdmx.istat.it host is deprecated "
            "and dimension order and naming CHANGED between the two, so old keys "
            "silently query the wrong series. /rest/v2/data is documented but not "
            "implemented, and endPeriod has an off-by-one-year bug on /rest/data."
        ),
        verified=True,
    ),
    _S(
        key="ine_spain",
        country="Spain",
        agency="INE (Tempus3)",
        base_url="https://servicios.ine.es/wstempus/js",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        coverage=("population", "labour", "prices", "economy"),
        gotchas=(
            "The language code (ES or EN, uppercase) is a path segment immediately "
            "after /js/, then the function name: /js/EN/DATOS_TABLA/{id}. Start from "
            "OPERACIONES_DISPONIBLES to discover ids. JSON only."
        ),
        verified=True,
    ),
    _S(
        key="gus_poland",
        country="Poland",
        agency="GUS Local Data Bank (BDL)",
        base_url="https://bdl.stat.gov.pl/api/v1",
        protocol=Protocol.REST_JSON,
        auth=Auth.KEY_FREE,
        env_var="GUS_BDL_API_KEY",
        signup_url="https://api.stat.gov.pl/Home/BdlApi",
        coverage=("population", "labour", "economy", "regional"),
        rate_limit="anonymous 5/s, 1,000/12h; registered 10/s, 5,000/12h",
        gotchas=(
            "The key is optional — anonymous works at a lower quota. When set it goes "
            "in an X-ClientId header, not a query parameter."
        ),
        verified=True,
    ),
    _S(
        key="ine_portugal",
        country="Portugal",
        agency="INE Portugal",
        base_url="https://www.ine.pt/ine/json_indicador/pindica.jsp",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        coverage=("population", "labour", "economy", "prices"),
        gotchas=(
            "Indicator-centric rather than a table tree: you must already know the "
            "numeric varcd and its dimension codes, found by switching the ine.pt "
            "database UI from 'Tree' to 'Codes'. lang values are uppercase PT/EN. "
            "Dim1 is conventionally time and Dim2 geography."
        ),
        verified=True,
    ),
    _S(
        key="statcube_austria",
        country="Austria",
        agency="Statistik Austria (STATcube)",
        base_url="https://statcubeapi.statistik.at/statistik.at/ext/statcube/rest/v1",
        protocol=Protocol.REST_JSON,
        auth=Auth.KEY_PAID,
        env_var="STATCUBE_API_KEY",
        coverage=("population", "labour", "economy"),
        gotchas=(
            "The API key requires a PAID STATcube subscription. Per-key hourly limits "
            "are readable at /rate_limit but not published. The free alternative is "
            "data.statistik.gv.at (~315 datasets, JSON metadata plus CSV). Base URL "
            "confirmed from Statistik Austria's own R package source, not from prose "
            "documentation."
        ),
        verified=False,
    ),
    _S(
        key="brreg_norway",
        country="Norway",
        agency="Brønnøysund Enhetsregisteret",
        base_url="https://data.brreg.no/enhetsregisteret/api",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        coverage=("company registry", "officers", "group structure", "update feeds"),
        rate_limit="no request-rate limit documented; (page+1)×size must be ≤ 10,000",
        gotchas=(
            "The best free company registry API in Europe and the right reference "
            "model for any registry adapter: it exposes roles (/enheter/{orgnr}/roller), "
            "group structure, incremental update feeds for syncing, and a full bulk "
            "download. Deep pagination past 10,000 returns HTTP 400 — use the download "
            "or the update feed for full sweeps."
        ),
        verified=True,
    ),
    _S(
        key="cvr_denmark",
        country="Denmark",
        agency="CVR (Erhvervsstyrelsen via Virk)",
        base_url="http://distribution.virk.dk/cvr-permanent",
        protocol=Protocol.REST_JSON,
        auth=Auth.KEY_FREE,
        env_var="CVR_BASIC_AUTH",
        coverage=("company registry", "officers", "production units", "employee bands"),
        gotchas=(
            "Outstanding data — full register including historical values with validity "
            "periods — behind an unusual door: it is a raw Elasticsearch endpoint "
            "taking query DSL, and access is granted manually by emailing "
            "cvrselvbetjening@erst.dk and signing a declaration. Do not confuse the "
            "official feed with the unofficial cvrapi.dk / cvr.dev wrappers."
        ),
        verified=True,
    ),
    _S(
        key="handelsregister_de",
        country="Germany",
        agency="Handelsregister / OffeneRegister",
        base_url="https://offeneregister.de",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        coverage=("company registry",),
        gotchas=(
            "There is NO official German company-registry API. handelsregister.de is "
            "free to use but has no REST interface and its terms restrict scraping. "
            "OffeneRegister offers a bulk dump of ~5.1M companies, but it was "
            "collected 2017-2019 and has never been updated — fine for historical "
            "lookups, wrong for current-state checks. Unternehmensregister bulk access "
            "is a paid product."
        ),
        verified=True,
    ),
]

# ── Asia-Pacific ──────────────────────────────────────────────────────────────

ASIA_PACIFIC = [
    _S(
        key="abs_australia",
        country="Australia",
        agency="Australian Bureau of Statistics (Data API)",
        base_url="https://data.api.abs.gov.au/rest",
        protocol=Protocol.SDMX,
        auth=Auth.NONE,
        docs_url="https://www.abs.gov.au/about/data-services/application-programming-interfaces-apis",
        coverage=("population", "labour", "prices", "census", "business"),
        gotchas=(
            "Mind the host order: api.data.abs.gov.au is the OLD host and its DNS no "
            "longer resolves at all. API keys were REMOVED on 29 Nov 2024 — it is now "
            "fully open. Still officially Beta, with availability explicitly not "
            "guaranteed. Query syntax is /rest/data/{dataflow}/{key} with "
            "dot-separated positional dimensions; ask for ?format=jsondata since the "
            "default is SDMX-ML."
        ),
        verified=True,
    ),
    _S(
        key="abs_indicator",
        country="Australia",
        agency="ABS Indicator API",
        base_url="https://indicator.api.abs.gov.au",
        protocol=Protocol.REST_JSON,
        auth=Auth.KEY_FREE,
        env_var="ABS_INDICATOR_API_KEY",
        coverage=("cpi", "labour force", "gdp", "retail trade", "building approvals"),
        gotchas=(
            "Production since Feb 2025, unlike the Data API. Key goes in an x-api-key "
            "header, but signup is manual: download the key-request form from the ABS "
            "page and email it to api.data@abs.gov.au. Deliberately small payloads "
            "across 19 headline datasets."
        ),
        verified=True,
    ),
    _S(
        key="abn_lookup",
        country="Australia",
        agency="ABN Lookup (Australian Business Register)",
        base_url="https://abr.business.gov.au",
        protocol=Protocol.REST_JSON,
        auth=Auth.KEY_FREE,
        env_var="ABN_LOOKUP_GUID",
        signup_url="https://abr.business.gov.au/Tools/WebServices",
        coverage=("company registry", "abn status", "gst registration", "business names"),
        gotchas=(
            "The authentication GUID goes in the querystring, not a header. Full "
            "search is SOAP; only a few endpoints (/json/AbnDetails.aspx) return "
            "JSONP. No officers, no financials. For bulk, ASIC publishes the whole "
            "2.1M-company register weekly as CSV on data.gov.au."
        ),
        verified=True,
    ),
    _S(
        key="stats_nz",
        country="New Zealand",
        agency="Stats NZ (Aotearoa Data Explorer)",
        base_url="https://api.data.stats.govt.nz/rest",
        protocol=Protocol.SDMX,
        auth=Auth.KEY_FREE,
        env_var="STATS_NZ_API_KEY",
        signup_url="https://portal.apis.stats.govt.nz/how-to-subscribe",
        coverage=("population", "labour", "economy", "trade"),
        gotchas=(
            "The legacy Stats NZ open-data API closed 30 Aug 2024 — do not build "
            "against api.stats.govt.nz/opendata. Auth is an Ocp-Apim-Subscription-Key "
            "header. In practice the host currently answers without a key, but the "
            "requirement is framed as fair use, so do not rely on that in production. "
            "Query form: /rest/data/{agency},{dataflow},{version}/{key}."
        ),
        verified=True,
    ),
    _S(
        key="estat_japan",
        country="Japan",
        agency="e-Stat",
        base_url="https://api.e-stat.go.jp/rest/3.0/app/json",
        protocol=Protocol.REST_JSON,
        auth=Auth.KEY_FREE,
        env_var="ESTAT_APP_ID",
        signup_url="https://www.e-stat.go.jp/en/mypage/user/preregister/",
        coverage=("population", "labour", "economy", "census"),
        rate_limit="100,000 records per getStatsData request; paginate with startPosition",
        gotchas=(
            "The parameter is appId, not apiKey. lang=E returns English but only a "
            "SUBSET of datasets is translated — metadata for many tables is "
            "Japanese-only, as is the full API specification. Look up statsDataId via "
            "getStatsList first."
        ),
        verified=True,
    ),
    _S(
        key="kosis_korea",
        country="South Korea",
        agency="KOSIS (Statistics Korea)",
        base_url="https://kosis.kr/openapi",
        protocol=Protocol.REST_JSON,
        auth=Auth.KEY_FREE,
        env_var="KOSIS_API_KEY",
        signup_url="https://kosis.kr/openapi/",
        coverage=("population", "labour", "economy", "prices"),
        rate_limit="200 calls/minute; 40,000 cells per request (200,000 for XLS)",
        gotchas=(
            "Effectively Korean-only: the portal, the usage application, the developer "
            "guide, error messages and most table metadata are all Korean, and there "
            "is no stable deep link to the key application. Plain HTTP was "
            "discontinued in Feb 2026 — HTTPS only. Budget for Korean-language handling."
        ),
        verified=True,
    ),
    _S(
        key="singstat",
        country="Singapore",
        agency="SingStat Table Builder",
        base_url="https://tablebuilder.singstat.gov.sg/api/table",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        coverage=("population", "labour", "economy", "trade"),
        gotchas=(
            "No key, no registration at all. Search resource ids via "
            "/resourceid?keyword=, then /tabledata/{resourceId}. Paginate with "
            "offset/limit. Rate limits are not published on any fetchable page."
        ),
        verified=True,
    ),
    _S(
        key="data_gov_sg",
        country="Singapore",
        agency="data.gov.sg",
        base_url="https://api-open.data.gov.sg",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        coverage=("real-time environment", "transport", "government datasets"),
        rate_limit="per 10s: realtime 6 keyless / 12 dev / 30 prod; downloads 2/4/10",
        gotchas=(
            "Two hosts: api-open for real-time and downloads, api-production for "
            "catalog and search. An optional free key (x-api-key header) raises the "
            "limits. Dataset downloads are a two-step initiate-then-poll flow."
        ),
        verified=True,
    ),
    _S(
        key="data_gov_in",
        country="India",
        agency="data.gov.in (OGD Platform)",
        base_url="https://api.data.gov.in/resource",
        protocol=Protocol.REST_JSON,
        auth=Auth.KEY_FREE,
        env_var="DATA_GOV_IN_API_KEY",
        signup_url="https://data.gov.in/user/register",
        coverage=("population", "economy", "agriculture", "health", "prices"),
        gotchas=(
            "A catalog of independently-published datasets rather than one coherent "
            "API: schema, freshness and quality vary wildly by ministry. Resource ids "
            "are opaque UUIDs discoverable only through the catalog UI. Per-key "
            "throttling exists but the numbers are not published anywhere fetchable."
        ),
        verified=True,
    ),
    _S(
        key="mospi_india",
        country="India",
        agency="MoSPI eSankhyiki",
        base_url="https://api.mospi.gov.in",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        coverage=("cpi", "labour force", "industrial production", "national accounts"),
        gotchas=(
            "Newer and much more coherent than data.gov.in for core Indian statistics "
            "(PLFS, CPI, IIP, ASI, NAS, HCES). Keyless in NSO's own official client. "
            "Young and still evolving; NSO also ships an MCP server for it."
        ),
        verified=True,
    ),
    _S(
        key="nbs_china",
        country="China",
        agency="National Bureau of Statistics",
        base_url="https://data.stats.gov.cn/easyquery.htm",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        coverage=("population", "economy", "industry"),
        gotchas=(
            "There is no official public API. This is the website's own internal "
            "endpoint, reverse-engineered and community-documented, with no docs, no "
            "versioning guarantee and no terms covering programmatic use. Live probes "
            "from US hosts return 403 on both Chinese and English endpoints, "
            "consistent with WAF and geo-filtering. Use a mirror such as DBnomics' NBS "
            "provider instead of building a production dependency here."
        ),
        verified=True,
    ),
]

# ── Latin America, Africa ─────────────────────────────────────────────────────

REST_OF_WORLD = [
    _S(
        key="ibge_sidra",
        country="Brazil",
        agency="IBGE SIDRA",
        base_url="https://apisidra.ibge.gov.br",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        coverage=("census", "population", "prices", "agriculture", "industry"),
        rate_limit="100,000 values per query",
        gotchas=(
            "Deepest Latin American coverage, down to municipality level. The path is "
            "a terse grammar: /values/t/{table}/n{level}/{codes}/v/{vars}/p/{periods}. "
            "The value count is the PRODUCT of elements across up to 9 dimensions, so "
            "compute it before querying or you will trip the cap constantly."
        ),
        verified=True,
    ),
    _S(
        key="ibge_servicodados",
        country="Brazil",
        agency="IBGE servicodados",
        base_url="https://servicodados.ibge.gov.br/api/v3/agregados",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        coverage=("population", "economy", "prices", "geography"),
        gotchas=(
            "The modern, versioned API that feeds SIDRA. Easier to consume than SIDRA "
            "but exposes fewer tables. Sibling APIs under /api/ cover localidades, "
            "malhas (geometries) and nomes."
        ),
        verified=True,
    ),
    _S(
        key="datos_gob_ar",
        country="Argentina",
        agency="INDEC / datos.gob.ar series API",
        base_url="https://apis.datos.gob.ar/series/api",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        coverage=("prices", "economy", "labour"),
        gotchas="Series-oriented; find ids through the datos.gob.ar catalog search first.",
    ),
    _S(
        key="statssa",
        country="South Africa",
        agency="Statistics South Africa",
        base_url="https://www.statssa.gov.za",
        protocol=Protocol.HTML,
        auth=Auth.NONE,
        coverage=("population", "labour", "economy"),
        gotchas=(
            "No general public API — publication downloads and the Nesstar portal "
            "only. For programmatic South African data, World Bank and ILOSTAT are the "
            "practical routes."
        ),
        verified=True,
    ),
]

# ── Multilateral ──────────────────────────────────────────────────────────────

MULTILATERAL = [
    _S(
        key="world_bank",
        country="Global",
        agency="World Bank Indicators API",
        base_url="https://api.worldbank.org/v2",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        docs_url="https://datahelpdesk.worldbank.org/knowledgebase/topics/125589",
        coverage=("macro", "population", "development", "poverty", "trade", "education"),
        rate_limit="undocumented but real; per_page caps at 32,767; 60 indicators per call",
        gotchas=(
            "The cheapest possible global spine: no key, 189 countries, one consistent "
            "schema. Pass format=json — the default is XML. The response is "
            "[metadata, rows], and treating it as a plain row list is the standard "
            "first bug. Errors come back as HTML or XML even when you asked for JSON. "
            "The trap that costs real debugging time: THROTTLING IS SIGNALLED AS A 502 "
            "HTML PAGE OR A READ TIMEOUT, never a 429. Under a burst, valid URLs start "
            "failing in ways that look like malformed requests — both the date=2022:2023 "
            "colon and the USA;BRA separator look guilty and are in fact fine. Space "
            "the requests instead of widening the retry budget."
        ),
        verified=True,
    ),
    _S(
        key="imf",
        country="Global",
        agency="IMF Data",
        base_url="https://api.imf.org/external/sdmx/2.1",
        protocol=Protocol.SDMX,
        auth=Auth.NONE,
        coverage=("macro", "balance of payments", "government finance", "trade"),
        gotchas=(
            "The legacy dataservices.imf.org SDMX-JSON API was RETIRED 5 Nov 2025 and "
            "its endpoints are dead — most client libraries and catalogs written "
            "before then are broken. Dataflow IDs are now agency-prefixed "
            "(IMF.STA/CPI) rather than flat. Data endpoints returned intermittent 503 "
            "'no healthy upstream' during verification while structure endpoints "
            "worked, so build retries. A 3.0 endpoint exists alongside this one."
        ),
        verified=True,
    ),
    _S(
        key="oecd",
        country="Global",
        agency="OECD Data Explorer",
        base_url="https://sdmx.oecd.org/public/rest",
        protocol=Protocol.SDMX,
        auth=Auth.NONE,
        coverage=("macro", "labour", "education", "health", "productivity"),
        rate_limit="60 data downloads per hour; VPN and anonymised traffic is blocked",
        gotchas=(
            "Gives you cross-country COMPARABLE series, which no national API can. "
            "stats.oecd.org is retired and dataflow ids were renamed wholesale in the "
            "migration. The agency id is a mandatory comma-separated path segment "
            "(OECD.SDD.NAD,DSD_NAAG@DF_NAAG_I,1.0), note the @ inside dataflow ids. "
            "Omitting the version takes 'latest', which can silently rename or drop "
            "dimensions. Cache aggressively — 60/hour is easy to exhaust."
        ),
        verified=True,
    ),
    _S(
        key="ilostat",
        country="Global",
        agency="ILOSTAT",
        base_url="https://sdmx.ilo.org/rest",
        protocol=Protocol.SDMX,
        auth=Auth.NONE,
        coverage=("labour", "employment", "wages", "hours", "informality"),
        gotchas=(
            "Dataflows follow ILO,DF_{indicator}. Wrong key dimensions return HTTP 422 "
            "rather than an empty result, which is unusually helpful. Content "
            "negotiation works: Accept: application/vnd.sdmx.data+json for JSON. For "
            "anything wide, the bulk backend at rplumber.ilo.org/data is far more "
            "practical than the query API."
        ),
        verified=True,
    ),
    _S(
        key="unsd_sdg",
        country="Global",
        agency="UN Statistics Division (SDG API)",
        base_url="https://unstats.un.org/sdgapi/v1/sdg",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        coverage=("sdg indicators", "development", "population", "environment"),
        gotchas=(
            "Full global SDG database with age, sex and location disaggregation. "
            "Series codes differ from indicator codes. Pivot endpoints return data as "
            "JSON strings nested inside JSON. Large pulls are easier through the POST "
            "CSV export than the paginated /Series/Data endpoint."
        ),
        verified=True,
    ),
    _S(
        key="who_gho",
        country="Global",
        agency="WHO Global Health Observatory",
        base_url="https://ghoapi.azureedge.net/api",
        protocol=Protocol.ODATA,
        auth=Auth.NONE,
        coverage=("health", "mortality", "disease burden", "health systems"),
        gotchas=(
            "Trivial to integrate. Indicator codes are non-obvious (WHOSIS_000001), so "
            "filter /api/Indicator by name first — and note the data endpoint is the "
            "indicator code directly on /api/, not under a /data/ path."
        ),
        verified=True,
    ),
    _S(
        key="unesco_uis",
        country="Global",
        agency="UNESCO Institute for Statistics",
        base_url="https://api.uis.unesco.org/api/public",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        coverage=("education", "science", "culture", "sdg4"),
        rate_limit="no rate limiting; hard cap 100,000 records per query",
        gotchas=(
            "The old apiportal.uis.unesco.org is completely dead. CORS is open to any "
            "origin. Pass an explicit version parameter and keep query-param ORDER "
            "consistent, because caching is keyed on the full URL. Larger pulls go "
            "through the BDDS bulk browser."
        ),
        verified=True,
    ),
    _S(
        key="comtrade",
        country="Global",
        agency="UN Comtrade",
        base_url="https://comtradeapi.un.org",
        protocol=Protocol.REST_JSON,
        auth=Auth.KEY_FREE,
        env_var="COMTRADE_API_KEY",
        signup_url="https://comtradedeveloper.un.org/",
        coverage=("merchandise trade", "services trade", "tariffs"),
        rate_limit="free tier 500 calls/day, 1 call/s, 100K records/call",
        gotchas=(
            "The /public/v1/preview/ endpoints need NO key at all and are ideal for "
            "prototyping, capped at 500 records and one period plus one product. The "
            "legacy comtrade.un.org/data endpoints were decommissioned in early 2023."
        ),
        verified=True,
    ),
    _S(
        key="wto",
        country="Global",
        agency="WTO Timeseries",
        base_url="https://api.wto.org/timeseries/v1",
        protocol=Protocol.REST_JSON,
        auth=Auth.KEY_FREE,
        env_var="WTO_API_KEY",
        signup_url="https://apiportal.wto.org/signup",
        coverage=("merchandise trade", "services trade", "tariffs", "non-tariff measures"),
        gotchas=(
            "Free key, sent as an Ocp-Apim-Subscription-Key header. The developer "
            "portal is an unscrapeable SPA, so per-product quotas are only visible "
            "after signing in. stats.wto.org has a query builder that generates "
            "request URLs for you."
        ),
        verified=True,
    ),
    _S(
        key="faostat",
        country="Global",
        agency="FAOSTAT",
        base_url="https://bulks-faostat.fao.org",
        protocol=Protocol.CSV,
        auth=Auth.NONE,
        coverage=("agriculture", "food security", "land use", "emissions", "prices"),
        gotchas=(
            "BREAKING, April 2026: the formerly open API now requires a JWT bearer "
            "token from FAO's developer portal, and tokens expire after 60 minutes. "
            "faostatservices.fao.org returns 401 on everything and the older "
            "fenixservices host is dead (HTTP 521). Bulk downloads at this base remain "
            "open with no auth, which makes them the reliable path for unattended "
            "pipelines. The authenticated base URL is documented only behind the "
            "portal sign-in."
        ),
        verified=True,
    ),
    _S(
        key="gleif",
        country="Global",
        agency="GLEIF (Legal Entity Identifier)",
        base_url="https://api.gleif.org/api/v1",
        protocol=Protocol.REST_JSON,
        auth=Auth.NONE,
        coverage=("legal entities", "ownership hierarchy", "registry cross-reference"),
        rate_limit="undocumented, no rate-limit headers returned; treat as fair use",
        gotchas=(
            "No key, no registration, ~3.4M entities, and a free parent/child "
            "ownership graph. The killer feature for cross-border work is that each "
            "record carries its registration authority and LOCAL REGISTRY ID, which is "
            "your join key to every national registry. Requires "
            "Accept: application/vnd.api+json and JSON:API bracketed filters. Covers "
            "only entities that obtained an LEI, so it complements registries rather "
            "than replacing them."
        ),
        verified=True,
    ),
    _S(
        key="opencorporates",
        country="Global",
        agency="OpenCorporates",
        base_url="https://api.opencorporates.com/v0.4",
        protocol=Protocol.REST_JSON,
        auth=Auth.KEY_PAID,
        env_var="OPENCORPORATES_API_KEY",
        signup_url="https://opencorporates.com/api_accounts/new",
        coverage=("company registry", "officers", "filings"),
        gotchas=(
            "Widest cross-jurisdiction coverage (~140 jurisdictions) but PAID in "
            "practice — plans start around £225/month and there is no general free "
            "tier. The API reference page still describes an old free tier; that text "
            "is stale and most pre-2023 catalogs repeat it. Free at-scale access is "
            "grant-based for journalists, NGOs and academics."
        ),
        verified=True,
    ),
]

CATALOG: list[DataSource] = [
    *NORTH_AMERICA,
    *EUROPE,
    *ASIA_PACIFIC,
    *REST_OF_WORLD,
    *MULTILATERAL,
]

BY_KEY: dict[str, DataSource] = {s.key: s for s in CATALOG}

#: Highest coverage per unit of integration effort. Start here.
RECOMMENDED_FIRST: tuple[str, ...] = (
    "world_bank",
    "gleif",
    "sec_edgar",
    "companies_house",
    "eurostat",
    "us_census",
    "fred",
    "statcan",
    "oecd",
    "comtrade",
    "france_recherche_entreprises",
    "brreg_norway",
    "abs_australia",
    "ibge_sidra",
    "estat_japan",
)


def get(key: str) -> DataSource:
    if key not in BY_KEY:
        raise KeyError(f"unknown source {key!r}; known: {', '.join(sorted(BY_KEY))}")
    return BY_KEY[key]


def by_country(country: str) -> list[DataSource]:
    needle = country.lower()
    return [s for s in CATALOG if needle in s.country.lower()]


def by_protocol(protocol: Protocol | str) -> list[DataSource]:
    value = protocol.value if isinstance(protocol, Protocol) else protocol
    return [s for s in CATALOG if s.protocol.value == value]


def by_coverage(topic: str) -> list[DataSource]:
    needle = topic.lower()
    return [s for s in CATALOG if any(needle in c.lower() for c in s.coverage)]


def ready() -> list[DataSource]:
    """Sources usable right now, with no further configuration."""
    return [s for s in CATALOG if s.configured]
