"""The Kimi client every agent in the swarm calls.

This is the consolidation of six half-implementations found across three sibling
repositories. Each had one or two of these behaviours; none had all of them:

* model fallback chain, tried in order
* exponential backoff per model before demoting to the next
* empty-content recovery — Kimi spends its budget on reasoning tokens and
  returns ``content: ""`` with ``finish_reason: length``. That is a budget
  problem, so double ``max_tokens`` and retry the *same* model first. Only if
  the doubled attempt is still empty do we fall back to the reasoning text.
* server-side web search on OpenRouter (``openrouter:web_search`` — note that
  the plain ``web_search`` tool type used elsewhere in the fleet is not valid on
  any provider and 400s on vLLM)
* per-call usage recorded to a shared run ledger with a hard spend ceiling
* structured-output mode with schema validation, not bare ``json.loads``
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import settings
from .budget import BudgetExceeded, Ledger
from .json_util import parse_json_loose

log = logging.getLogger("uscrape.llm")

# Transient upstream conditions worth another attempt on the same model.
_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """Every model in the chain failed."""


@dataclass
class Completion:
    text: str
    model: str
    usage: dict
    cost_usd: float
    attempts: int
    finish_reason: str | None = None
    citations: list[dict] | None = None
    used_reasoning_fallback: bool = False


def web_search_tool(max_results: int | None = None, context_size: str | None = None) -> dict:
    """OpenRouter's server-side web search tool.

    OpenRouter runs the search itself and folds the results into the prompt, so
    there is no client-side tool loop to write. The tool type must be exactly
    ``openrouter:web_search``.
    """
    return {
        "type": "openrouter:web_search",
        "parameters": {
            "max_results": max_results or settings.search_max_results,
            "search_context_size": context_size or settings.search_context_size,
        },
    }


class KimiClient:
    """Async chat client with a model fallback chain and a shared spend ledger.

    Use as an async context manager so the underlying connection pool is closed:

        async with KimiClient(ledger) as llm:
            out = await llm.complete("...", grounded=True)
    """

    def __init__(
        self,
        ledger: Ledger | None = None,
        *,
        models: list[str] | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        concurrency: int | None = None,
    ) -> None:
        self.ledger = ledger or Ledger()
        self.models = models or settings.models
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.api_key = api_key or settings.api_key
        if not self.api_key:
            raise LLMError(
                "no LLM key configured — set OPENROUTER_API_KEY (or KIMI_API_KEY) in .env"
            )
        self._sem = asyncio.Semaphore(concurrency or settings.llm_concurrency)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.agent_timeout_s, connect=15.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                # OpenRouter attribution; harmless on other providers.
                "HTTP-Referer": settings.referer,
                "X-Title": settings.app_title,
            },
        )

    async def __aenter__(self) -> KimiClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def check_credits(self) -> dict | None:
        """Pre-flight balance check. Returns None when the endpoint is unavailable.

        Never blocks a run: a billing-endpoint hiccup must not stop research.
        It logs loudly and lets the caller decide.
        """
        if not settings.is_openrouter:
            return None
        try:
            r = await self._client.get(f"{self.base_url}/credits", timeout=15.0)
            r.raise_for_status()
            data = r.json().get("data", {})
            total = float(data.get("total_credits") or 0)
            used = float(data.get("total_usage") or 0)
            remaining = total - used
            if remaining < settings.min_credits_usd:
                log.error(
                    "OpenRouter balance $%.2f is below the $%.2f floor — the swarm will "
                    "start but may die mid-run",
                    remaining,
                    settings.min_credits_usd,
                )
            return {"total": total, "usage": used, "remaining": remaining}
        except Exception as exc:  # noqa: BLE001 - never fatal
            log.warning("credit check unavailable (%s); continuing", exc)
            return None

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        grounded: bool = False,
        json_mode: bool = False,
        max_tokens: int = 8000,
        temperature: float = 0.3,
        label: str = "agent",
        models: list[str] | None = None,
        reasoning: dict | None = None,
    ) -> Completion:
        """One chat completion, walking the model chain until something works."""
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        chain = models or self.models
        last_error: Exception | None = None

        async with self._sem:
            for model in chain:
                budget = max_tokens
                for attempt in range(1, settings.max_attempts + 1):
                    try:
                        return await self._one_call(
                            model=model,
                            messages=messages,
                            grounded=grounded,
                            json_mode=json_mode,
                            max_tokens=budget,
                            temperature=temperature,
                            attempt=attempt,
                            label=label,
                            reasoning=reasoning,
                        )
                    except BudgetExceeded:
                        raise  # a spend ceiling is never retryable
                    except _EmptyContent as exc:
                        # Reasoning ate the completion budget. Doubling is the
                        # fix; demoting the model is not.
                        last_error = exc
                        budget = min(budget * 2, 60_000)
                        log.warning(
                            "[%s] %s returned empty content (finish=%s); retrying at "
                            "max_tokens=%d",
                            label,
                            model,
                            exc.finish_reason,
                            budget,
                        )
                    except Exception as exc:  # noqa: BLE001
                        last_error = exc
                        await self.ledger.note_failure()
                        if attempt < settings.max_attempts:
                            delay = 2**attempt
                            log.warning(
                                "[%s] %s attempt %d/%d failed (%s); retrying in %ds",
                                label,
                                model,
                                attempt,
                                settings.max_attempts,
                                exc,
                                delay,
                            )
                            await asyncio.sleep(delay)
                log.warning("[%s] %s exhausted; falling back", label, model)

        raise LLMError(f"[{label}] every model in {chain} failed; last error: {last_error}")

    async def complete_json(
        self,
        prompt: str,
        *,
        system: str | None = None,
        grounded: bool = False,
        max_tokens: int = 8000,
        temperature: float = 0.2,
        label: str = "agent",
        repair_attempts: int = 1,
    ) -> tuple[Any, Completion]:
        """Completion whose output is parsed as JSON, with a repair round-trip.

        On an unparseable response the model is shown its own broken output and
        asked for JSON only. That recovers the common "prefixed a sentence"
        failure without burning a whole fresh research call.
        """
        comp = await self.complete(
            prompt,
            system=system,
            grounded=grounded,
            json_mode=True,
            max_tokens=max_tokens,
            temperature=temperature,
            label=label,
        )
        try:
            return parse_json_loose(comp.text), comp
        except Exception as exc:
            if repair_attempts <= 0:
                raise
            log.warning("[%s] JSON parse failed (%s); attempting repair", label, exc)
            repair = await self.complete(
                "The following was supposed to be a single valid JSON document but "
                "could not be parsed. Return the corrected JSON and nothing else — "
                "no prose, no code fences.\n\n" + comp.text[:20_000],
                json_mode=True,
                max_tokens=max_tokens,
                temperature=0.0,
                label=f"{label}:repair",
            )
            return parse_json_loose(repair.text), repair

    async def _one_call(
        self,
        *,
        model: str,
        messages: list[dict],
        grounded: bool,
        json_mode: bool,
        max_tokens: int,
        temperature: float,
        attempt: int,
        label: str,
        reasoning: dict | None = None,
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if reasoning is not None:
            # OpenRouter reasoning control — heavy thinkers (kimi-k3) otherwise
            # burn the whole max_tokens budget before emitting content.
            payload["reasoning"] = reasoning
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if grounded:
            if not settings.is_openrouter:
                # vLLM-backed providers (NIM) only accept function tools and 400
                # on server-side search. Silently dropping the tool would return
                # ungrounded output that looks grounded.
                raise LLMError(
                    f"[{label}] grounded call requested but base_url {self.base_url} "
                    "does not support server-side web search; use OpenRouter"
                )
            payload["tools"] = [web_search_tool()]

        resp = await self._client.post(f"{self.base_url}/chat/completions", json=payload)
        if resp.status_code in _RETRY_STATUS:
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code}: {resp.text[:300]}",
                request=resp.request,
                response=resp,
            )
        resp.raise_for_status()
        data = resp.json()

        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        finish = choice.get("finish_reason")
        usage = data.get("usage") or {}
        cost = await self.ledger.record(model, usage)

        text = (msg.get("content") or "").strip()
        used_reasoning = False
        if not text:
            if attempt < settings.max_attempts:
                raise _EmptyContent(finish_reason=finish)
            # Out of retries: salvage the reasoning trace rather than lose the call.
            reasoning = msg.get("reasoning") or ""
            if not reasoning and msg.get("reasoning_details"):
                reasoning = "\n".join(
                    d.get("text", "") for d in msg["reasoning_details"] if isinstance(d, dict)
                )
            text = reasoning.strip()
            used_reasoning = bool(text)
            if not text:
                raise _EmptyContent(finish_reason=finish)

        return Completion(
            text=text,
            model=model,
            usage=usage,
            cost_usd=cost,
            attempts=attempt,
            finish_reason=finish,
            citations=msg.get("annotations") or data.get("citations"),
            used_reasoning_fallback=used_reasoning,
        )


class _EmptyContent(RuntimeError):
    def __init__(self, finish_reason: str | None = None) -> None:
        super().__init__(f"empty content (finish_reason={finish_reason})")
        self.finish_reason = finish_reason
