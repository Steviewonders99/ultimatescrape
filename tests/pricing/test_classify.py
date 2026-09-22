import json
import os
import pathlib

import pytest

from tests.pricing.conftest import requires_pg
from ultimatescrape.pricing import classify, ddl
from ultimatescrape.pricing.classify import build_prompt, classify_new, parse_response

KEYS = {"llm-eval-rlhf", "search-rating", "corporate-role", "other"}

GOLDEN = pathlib.Path(__file__).parent / "golden_listings.json"
requires_llm = pytest.mark.skipif(
    not os.getenv("OPENROUTER_API_KEY"), reason="no LLM key in env")


class StubLLM:
    """Async context manager standing in for KimiClient — cheap, deterministic.

    Returns one canned ``(data, meta)`` response per ``complete_json`` call, in
    the order queued, so a test controls exactly what each batch "sees".
    """

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls: list[str] = []
        self.prompts: list[str] = []

    async def __aenter__(self) -> "StubLLM":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def complete_json(self, prompt: str, **kwargs):
        self.calls.append(kwargs.get("label", ""))
        self.prompts.append(prompt)
        return self._responses.pop(0), None


def test_parse_valid_response():
    data = {"results": [
        {"i": 0, "market_type": "search-rating", "confidence": 0.92,
         "country_code": "US", "language": "", "expertise_level": ""},
        {"i": 1, "market_type": "llm-eval-rlhf", "confidence": 0.8,
         "country_code": "", "language": "de", "expertise_level": "expert"},
    ]}
    out = parse_response(data, n=2, valid_keys=KEYS)
    assert out[0]["market_type"] == "search-rating"
    assert out[1]["attrs"]["language"] == "de"


def test_unknown_key_downgrades_to_other():
    data = {"results": [{"i": 0, "market_type": "made-up", "confidence": 0.9}]}
    out = parse_response(data, n=1, valid_keys=KEYS)
    assert out[0]["market_type"] == "other"
    assert out[0]["confidence"] <= 0.3


def test_missing_index_downgrades_to_other():
    out = parse_response({"results": []}, n=1, valid_keys=KEYS)
    assert out[0]["market_type"] == "other"


def test_prompt_contains_every_listing_and_contract():
    p = build_prompt([{"title": "Search Rater", "location": "US",
                       "platform": "telus", "description_excerpt": "rate ads"}])
    assert "Search Rater" in p and '"results"' in p


async def _insert_listing(pool, *, external_id: str, title: str, platform: str = "outlier") -> int:
    return await pool.fetchval(
        """
        INSERT INTO competitor_listing
          (platform, company, external_id, title, location_raw,
           description_excerpt, first_seen_at, last_seen_at, content_hash)
        VALUES ($1,$2,$3,$4,$5,$6, now(), now(), $7)
        RETURNING id
        """,
        platform, platform.title(), external_id, title, "US",
        f"{title} description", f"hash-{external_id}",
    )


@requires_pg
async def test_classify_new_writes_class_rows(pool):
    await ddl.apply(pool)
    id0 = await _insert_listing(pool, external_id="a1", title="Search Rater")
    id1 = await _insert_listing(pool, external_id="a2", title="LLM Eval Trainer")
    id2 = await _insert_listing(pool, external_id="a3", title="Mystery Role")

    stub = StubLLM([{"results": [
        {"i": 0, "market_type": "search-rating", "confidence": 0.9,
         "country_code": "US", "language": "", "expertise_level": ""},
        {"i": 1, "market_type": "llm-eval-rlhf", "confidence": 0.85,
         "country_code": "", "language": "en", "expertise_level": "entry"},
        {"i": 2, "market_type": "not-a-real-key", "confidence": 0.7},
    ]}])

    summary = await classify_new(pool, llm=stub)
    assert summary == {"classified": 2, "unclassifiable": 1, "batches": 1}
    assert stub.calls == ["pricing-classify"]

    rows = {r["listing_id"]: r for r in await pool.fetch(
        "SELECT listing_id, market_type, confidence, attrs FROM competitor_listing_class"
    )}
    assert rows[id0]["market_type"] == "search-rating"
    assert json.loads(rows[id1]["attrs"])["language"] == "en"
    # unknown key downgraded — never trust an answer outside the taxonomy
    assert rows[id2]["market_type"] == "other"
    assert rows[id2]["confidence"] <= 0.3

    # already-classified listings are never re-sent to the model
    again = await classify_new(pool, llm=StubLLM([{"results": []}]))
    assert again == {"classified": 0, "unclassifiable": 0, "batches": 0}


@requires_pg
async def test_classify_new_limit_and_no_listings(pool):
    await ddl.apply(pool)
    assert await classify_new(pool, llm=StubLLM([])) == {
        "classified": 0, "unclassifiable": 0, "batches": 0}

    await _insert_listing(pool, external_id="b1", title="Role One")
    await _insert_listing(pool, external_id="b2", title="Role Two")

    stub = StubLLM([{"results": [
        {"i": 0, "market_type": "other", "confidence": 0.5},
    ]}])
    summary = await classify_new(pool, limit=1, llm=stub)
    assert summary["batches"] == 1
    n = await pool.fetchval("SELECT count(*) FROM competitor_listing_class")
    assert n == 1


@requires_pg
async def test_classify_new_reads_taxonomy_from_db_not_ddl_seed(pool):
    """The prompt must be built from the live market_taxonomy table, not the
    ddl.py seed — a DB edit to a description must reach the model without a
    code change. Proven by mutating one row post-seed and inspecting the
    exact prompt text the (stubbed) LLM received."""
    await ddl.apply(pool)
    marker = "CUSTOM-DB-ONLY-MARKER-9f2 for search rating work"
    await pool.execute(
        "UPDATE market_taxonomy SET description = $1 WHERE key = 'search-rating'",
        marker,
    )
    await _insert_listing(pool, external_id="c1", title="Some Role")

    stub = StubLLM([{"results": [
        {"i": 0, "market_type": "search-rating", "confidence": 0.9},
    ]}])
    await classify_new(pool, llm=stub)

    assert len(stub.prompts) == 1
    assert marker in stub.prompts[0]
    from ultimatescrape.pricing.ddl import TAXONOMY_SEED
    stale_seed_text = next(d for k, _, d in TAXONOMY_SEED if k == "search-rating")
    assert stale_seed_text not in stub.prompts[0]


@requires_llm
async def test_golden_agreement_gate():
    from ultimatescrape.llm.client import KimiClient
    from ultimatescrape.pricing.classify import SYSTEM
    from ultimatescrape.pricing.ddl import TAXONOMY_SEED

    golden = json.loads(GOLDEN.read_text())
    valid = {k for k, _, _ in TAXONOMY_SEED}
    hits = 0
    finish_reasons: list[str | None] = []
    # Exercises the PRODUCTION batch size, not a hard-coded one — classify_new
    # and this gate must always run the same batch shape.
    async with KimiClient() as llm:
        for start in range(0, len(golden), classify.BATCH_SIZE):
            batch = golden[start:start + classify.BATCH_SIZE]
            data, meta = await llm.complete_json(
                build_prompt(batch), system=SYSTEM, max_tokens=classify.MAX_TOKENS,
                temperature=0.0, label="pricing-classify-golden")
            finish_reasons.append(getattr(meta, "finish_reason", None))
            for g, r in zip(batch, parse_response(data, len(batch), valid)):
                hits += r["market_type"] == g["expected"]

    truncated = [fr for fr in finish_reasons if fr == "length"]
    print(f"\nfinish_reasons per batch: {finish_reasons}")
    print(f"golden agreement: {hits}/{len(golden)} = {hits / len(golden):.1%}")
    assert not truncated, (
        f"model truncated {len(truncated)}/{len(finish_reasons)} batches "
        f"(finish_reason='length') — accuracy numbers below are not trustworthy "
        f"until this clears: {finish_reasons}"
    )
    assert hits / len(golden) >= 0.90, f"golden agreement {hits}/{len(golden)}"
