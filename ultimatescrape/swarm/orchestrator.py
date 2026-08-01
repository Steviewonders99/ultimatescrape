"""The swarm runner.

Pipeline, in order:

  1. pre-flight   credit check, backend health, work-matrix expansion, resume scan
  2. research     targets × dimensions fanned out under a semaphore, each result
                  checkpointed to disk the instant it lands
  3. merge        dedupe with corroboration counting and field back-fill
  4. validate     liveness-check every URL the agents claimed (deterministic)
  5. verify       N adversarial verifiers per finding, each with a distinct lens
  6. synthesize   one judgement pass over the verified set
  7. report       markdown + machine-readable JSON

Steps 4 and 5 are the difference between a swarm and a plausible-text generator.
Step 4 in particular is free, deterministic, and catches the failure mode that
does the most damage: confidently cited URLs that do not exist.

Every stage is skippable and every stage checkpoints, so a run can be killed and
resumed at any point with ``resume=True``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from ..fetch.http import Fetcher
from ..llm.budget import BudgetExceeded, Ledger
from ..llm.client import KimiClient, LLMError
from ..llm.json_util import JsonRecoveryError, coerce_dict
from ..store.run import RunStore, utcnow
from . import prompts
from .merge import merge_findings
from .spec import Dimension, SwarmSpec, Target

log = logging.getLogger("uscrape.swarm")

ProgressFn = Callable[[str, dict], None]


@dataclass
class SwarmResult:
    run_id: str
    spec: dict
    findings: list[dict] = field(default_factory=list)
    units: list[dict] = field(default_factory=list)
    synthesis: str = ""
    ledger: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "spec": self.spec,
            "stats": self.stats,
            "ledger": self.ledger,
            "synthesis": self.synthesis,
            "findings": self.findings,
            "errors": self.errors,
        }


class Swarm:
    def __init__(
        self,
        *,
        store: RunStore | None = None,
        ledger: Ledger | None = None,
        llm: KimiClient | None = None,
        fetcher: Fetcher | None = None,
        progress: ProgressFn | None = None,
    ) -> None:
        self.ledger = ledger or Ledger()
        self.store = store
        self._llm = llm
        self._owns_llm = llm is None
        self._fetcher = fetcher
        self._owns_fetcher = fetcher is None
        self.progress = progress or (lambda event, data: None)

    async def __aenter__(self) -> Swarm:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_llm and self._llm is not None:
            await self._llm.aclose()
            self._llm = None
        if self._owns_fetcher and self._fetcher is not None:
            await self._fetcher.aclose()
            self._fetcher = None

    @property
    def llm(self) -> KimiClient:
        if self._llm is None:
            self._llm = KimiClient(self.ledger)
        return self._llm

    @property
    def fetcher(self) -> Fetcher:
        if self._fetcher is None:
            self._fetcher = Fetcher()
        return self._fetcher

    # ── main entry point ──────────────────────────────────────────────────────

    async def run(
        self,
        spec: SwarmSpec,
        *,
        resume: bool = False,
        validate_urls: bool = True,
        synthesize: bool = True,
        deadline_s: float | None = None,
    ) -> SwarmResult:
        started = time.monotonic()
        deadline = deadline_s or settings.run_deadline_s

        store = self.store or RunStore.create(spec.topic)
        self.store = store
        store.write_manifest(
            topic=spec.topic, spec=spec.as_dict(), status="running", started_at=utcnow()
        )

        credits = await self.llm.check_credits()
        if credits:
            log.info("OpenRouter balance: $%.2f remaining", credits["remaining"])

        units = spec.work_units()
        keys = [spec.unit_key(t, d) for t, d in units]
        todo = [
            (t, d)
            for (t, d), key in zip(units, keys)
            if not (resume and store.has_unit(key))
        ]
        skipped = len(units) - len(todo)
        log.info(
            "swarm '%s': %d targets × %d dimensions = %d units (%d already done, %d to run)",
            spec.topic,
            len(spec.targets),
            len(spec.dimensions),
            len(units),
            skipped,
            len(todo),
        )
        self.progress(
            "plan",
            {"units": len(units), "todo": len(todo), "resumed": skipped, "run_id": store.run_id},
        )

        errors: list[dict] = []
        aborted = False
        if todo:
            aborted = await self._research(spec, todo, store, errors, deadline)

        raw_units = store.load_units()
        findings = self._collect(spec, raw_units)
        log.info("collected %d raw findings from %d units", len(findings), len(raw_units))

        merged = merge_findings(findings, dedupe_fields=spec.dedupe_fields)
        log.info("merged to %d unique findings", len(merged))
        self.progress("merged", {"raw": len(findings), "unique": len(merged)})

        if validate_urls and merged:
            await self._validate_urls(spec, merged)

        if spec.verifier_votes > 0 and merged and not aborted:
            try:
                await self._verify(spec, merged)
            except BudgetExceeded as exc:
                log.error("verification stopped: %s", exc)
                errors.append({"stage": "verify", "error": str(exc)})

        synthesis = ""
        if synthesize and merged and not aborted:
            try:
                synthesis = await self._synthesize(spec, merged)
            except (LLMError, BudgetExceeded, JsonRecoveryError) as exc:
                log.error("synthesis failed: %s", exc)
                errors.append({"stage": "synthesis", "error": str(exc)})

        stats = {
            "targets": len(spec.targets),
            "dimensions": len(spec.dimensions),
            "units_total": len(units),
            "units_completed": sum(1 for u in raw_units if not u.get("error")),
            "units_failed": sum(1 for u in raw_units if u.get("error")),
            "units_resumed": skipped,
            "findings_raw": len(findings),
            "findings_unique": len(merged),
            "findings_verified": sum(1 for f in merged if f.get("_verdict") == "supported"),
            "findings_refuted": sum(1 for f in merged if f.get("_verdict") == "refuted"),
            "urls_dead": sum(1 for f in merged if str(f.get("_url_status", "")).startswith("dead")),
            "elapsed_s": round(time.monotonic() - started, 1),
            "aborted": aborted,
        }

        result = SwarmResult(
            run_id=store.run_id,
            spec=spec.as_dict(),
            findings=merged,
            units=raw_units,
            synthesis=synthesis,
            ledger=self.ledger.summary(),
            stats=stats,
            errors=errors,
        )
        store.write_manifest(
            status="aborted" if aborted else "complete",
            finished_at=utcnow(),
            stats=stats,
            ledger=result.ledger,
            errors=errors,
        )
        store._atomic_write(store.root / "findings.json", {"findings": merged})
        self.progress("done", stats)
        return result

    # ── stage 2: research fan-out ─────────────────────────────────────────────

    async def _research(
        self,
        spec: SwarmSpec,
        todo: Sequence[tuple[Target, Dimension]],
        store: RunStore,
        errors: list[dict],
        deadline_s: float,
    ) -> bool:
        """Returns True if the run was cut short by budget or deadline."""
        sem = asyncio.Semaphore(settings.llm_concurrency)
        completed = 0
        aborted = False
        total = len(todo)

        async def one(target: Target, dimension: Dimension) -> None:
            nonlocal completed
            key = spec.unit_key(target, dimension)
            async with sem:
                payload: dict[str, Any]
                try:
                    prompt = dimension.render(target, spec.topic) + prompts.JSON_CONTRACT_FOOTER.format(
                        contract=spec.output_contract
                    )
                    data, comp = await self.llm.complete_json(
                        prompt,
                        system=spec.system_prompt,
                        grounded=dimension.grounded,
                        max_tokens=dimension.max_tokens,
                        temperature=dimension.temperature,
                        label=key,
                    )
                    payload = coerce_dict(data, context=key)
                    payload["_meta"] = {
                        "target": target.key,
                        "target_label": target.label,
                        "dimension": dimension.key,
                        "model": comp.model,
                        "usage": comp.usage,
                        "cost_usd": round(comp.cost_usd, 6),
                        "attempts": comp.attempts,
                        "reasoning_fallback": comp.used_reasoning_fallback,
                    }
                except BudgetExceeded:
                    raise
                except Exception as exc:  # noqa: BLE001
                    payload = {
                        "error": f"{type(exc).__name__}: {exc}",
                        "_meta": {"target": target.key, "dimension": dimension.key},
                    }
                    errors.append({"unit": key, "error": str(exc)})
                    log.warning("[%s] failed: %s", key, exc)

                store.save_unit(key, payload)
                completed += 1
                self.progress(
                    "unit",
                    {
                        "key": key,
                        "done": completed,
                        "total": total,
                        "ok": "error" not in payload,
                        "cost_usd": round(self.ledger.cost_usd, 4),
                    },
                )

        tasks = [asyncio.create_task(one(t, d)) for t, d in todo]
        try:
            done, pending = await asyncio.wait(tasks, timeout=deadline_s)
            if pending:
                aborted = True
                log.error(
                    "run deadline (%ss) hit with %d units outstanding; keeping %d completed",
                    deadline_s,
                    len(pending),
                    completed,
                )
                errors.append({"stage": "research", "error": f"deadline: {len(pending)} units unrun"})
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                exc = task.exception()
                if isinstance(exc, BudgetExceeded):
                    aborted = True
                    log.error("budget ceiling reached: %s", exc)
                    errors.append({"stage": "research", "error": str(exc)})
        except BudgetExceeded as exc:
            aborted = True
            errors.append({"stage": "research", "error": str(exc)})
            log.error("budget ceiling reached: %s", exc)
        return aborted

    # ── stage 3: collect ──────────────────────────────────────────────────────

    @staticmethod
    def _collect(spec: SwarmSpec, units: list[dict]) -> list[dict]:
        out: list[dict] = []
        for unit in units:
            if unit.get("error"):
                continue
            meta = unit.get("_meta") or {}
            agent = f"{meta.get('target', '?')}::{meta.get('dimension', '?')}"
            items = unit.get(spec.findings_key)
            if isinstance(items, dict):
                items = [items]
            if not isinstance(items, list):
                # The agent answered but used a different key. Salvage the first
                # list of objects rather than discarding a paid-for call.
                items = next(
                    (
                        v
                        for k, v in unit.items()
                        if not k.startswith("_")
                        and isinstance(v, list)
                        and v
                        and isinstance(v[0], dict)
                    ),
                    [],
                )
            for item in items:
                if isinstance(item, dict):
                    out.append(
                        {
                            **item,
                            "_agent": agent,
                            "_target": meta.get("target"),
                            "_dimension": meta.get("dimension"),
                        }
                    )
        return out

    # ── stage 4: deterministic URL validation ─────────────────────────────────

    async def _validate_urls(self, spec: SwarmSpec, findings: list[dict]) -> None:
        urls: set[str] = set()
        for finding in findings:
            for field_name in spec.url_fields:
                value = finding.get(field_name)
                if isinstance(value, str) and value.startswith("http"):
                    urls.add(value)
                elif isinstance(value, list):
                    urls.update(v for v in value if isinstance(v, str) and v.startswith("http"))
        if not urls:
            return

        log.info("validating %d claimed URLs", len(urls))
        statuses = await self.fetcher.check_urls(sorted(urls))
        dead = sum(1 for s in statuses.values() if s.startswith("dead") or s == "invalid")

        for finding in findings:
            per_field = {}
            for field_name in spec.url_fields:
                value = finding.get(field_name)
                if isinstance(value, str) and value in statuses:
                    per_field[field_name] = statuses[value]
            if per_field:
                finding["_url_checks"] = per_field
                primary = next(
                    (per_field[f] for f in spec.url_fields if f in per_field), None
                )
                finding["_url_status"] = primary

        log.info("URL validation: %d of %d unreachable", dead, len(urls))
        self.progress("urls", {"checked": len(urls), "dead": dead})

    # ── stage 5: adversarial verification ─────────────────────────────────────

    async def _verify(self, spec: SwarmSpec, findings: list[dict]) -> None:
        sem = asyncio.Semaphore(settings.llm_concurrency)
        lenses = spec.verifier_lenses[: spec.verifier_votes] or spec.verifier_lenses[:1]
        need = max(1, (len(lenses) // 2) + 1)
        log.info("verifying %d findings with %d lenses each", len(findings), len(lenses))

        async def vote(finding: dict, lens: str) -> dict | None:
            async with sem:
                try:
                    data, _ = await self.llm.complete_json(
                        prompts.verifier_prompt(finding, lens, spec.topic),
                        system=prompts.VERIFIER_SYSTEM,
                        grounded=True,
                        max_tokens=2000,
                        temperature=0.0,
                        label="verify",
                    )
                    return coerce_dict(data, context="verdict")
                except BudgetExceeded:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.debug("verifier failed: %s", exc)
                    return None

        async def judge(finding: dict) -> None:
            verdicts = await asyncio.gather(*(vote(finding, lens) for lens in lenses))
            live = [v for v in verdicts if v]
            if not live:
                finding["_verdict"] = "unverified"
                return
            supporting = sum(1 for v in live if v.get("supported"))
            finding["_verdict"] = "supported" if supporting >= need else "refuted"
            finding["_verdict_votes"] = f"{supporting}/{len(live)}"
            problems = [p for v in live for p in (v.get("problems") or [])]
            if problems:
                finding["_problems"] = problems[:6]
            # A URL that does not resolve overrides any amount of model agreement.
            if str(finding.get("_url_status", "")).startswith("dead"):
                finding["_verdict"] = "refuted"
                finding.setdefault("_problems", []).insert(
                    0, f"primary URL unreachable ({finding['_url_status']})"
                )

        await asyncio.gather(*(judge(f) for f in findings))
        supported = sum(1 for f in findings if f.get("_verdict") == "supported")
        log.info("verification: %d supported, %d refuted", supported, len(findings) - supported)
        self.progress("verified", {"supported": supported, "total": len(findings)})

    # ── stage 6: synthesis ────────────────────────────────────────────────────

    async def _synthesize(self, spec: SwarmSpec, findings: list[dict]) -> str:
        import json

        ranked = sorted(
            findings,
            key=lambda f: (
                f.get("_verdict") != "supported",
                -f.get("_corroborations", 1),
            ),
        )
        payload = json.dumps(ranked[:200], indent=2, ensure_ascii=False, default=str)[:180_000]
        comp = await self.llm.complete(
            prompts.synthesis_prompt(spec.topic, payload, spec.synthesis_prompt),
            system=prompts.SYNTHESIS_SYSTEM,
            grounded=False,
            max_tokens=16_000,
            temperature=0.4,
            label="synthesis",
        )
        return comp.text
