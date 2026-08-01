"""What a swarm run is: targets × dimensions, plus the contract each agent returns.

A *target* is a thing being researched (a country, a company, a city, a keyword).
A *dimension* is an angle of research applied to targets (competitors, hiring
signals, regulation, pricing). The work matrix is their product, which is where
the scale comes from: 40 targets × 8 dimensions is 320 independent agents, and
each one is small, cheap, separately cacheable, and separately retryable.

That decomposition is the single biggest quality lever in the whole system. One
giant prompt asking about 40 countries returns shallow, evenly-hedged text. Three
hundred small prompts each asking one question about one country return specifics.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Target:
    key: str
    label: str
    context: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def of(cls, value: str | dict | Target) -> Target:
        if isinstance(value, Target):
            return value
        if isinstance(value, dict):
            key = value.get("key") or value.get("label") or value.get("name") or ""
            return cls(key=key, label=value.get("label") or key, context=value)
        return cls(key=value, label=value)


@dataclass
class Dimension:
    """One research angle.

    ``prompt`` is a format string receiving ``target``, ``label``, ``topic`` and
    every key in the target's context. ``activates_when`` skips the dimension for
    targets it does not apply to — the cheapest possible cost control, since a
    skipped agent costs nothing.
    """

    key: str
    prompt: str
    why: str = ""
    output_keys: tuple[str, ...] = ()
    grounded: bool = True
    max_tokens: int = 8000
    temperature: float = 0.3
    activates_when: Callable[[Target], bool] | None = None

    def applies_to(self, target: Target) -> bool:
        return self.activates_when is None or bool(self.activates_when(target))

    def render(self, target: Target, topic: str) -> str:
        fields = {
            "target": target.key,
            "label": target.label,
            "topic": topic,
            **target.context,
        }
        try:
            return self.prompt.format(**fields)
        except KeyError as exc:
            raise KeyError(
                f"dimension {self.key!r} references {exc} which is not in the target context "
                f"for {target.key!r}; available: {sorted(fields)}"
            ) from exc


@dataclass
class SwarmSpec:
    """A complete, serialisable description of a run."""

    topic: str
    targets: list[Target]
    dimensions: list[Dimension]
    system_prompt: str
    #: JSON contract appended to every agent prompt.
    output_contract: str
    #: Where findings live in each agent's JSON, e.g. "findings" or "vendors".
    findings_key: str = "findings"
    #: Fields used to dedupe findings across agents. First non-empty wins.
    dedupe_fields: tuple[str, ...] = ("url", "name", "title")
    #: Fields whose values are URLs to liveness-check.
    url_fields: tuple[str, ...] = ("url", "website", "source_url", "linkedin")
    #: How many independent verifier agents adjudicate each finding. 0 disables.
    verifier_votes: int = 0
    #: Distinct lenses for verifiers. Diversity beats redundancy — three
    #: identical skeptics agree with each other, three different ones don't.
    verifier_lenses: tuple[str, ...] = (
        "factual accuracy — is every claim supported by a real, current source?",
        "recency — is this current as of today, or stale data presented as live?",
        "specificity — is this a concrete verifiable fact or a hedged generality?",
    )
    synthesis_prompt: str | None = None
    notes: str = ""
    #: Restricts the work matrix to these unit keys. Set on rehydration so a
    #: resumed run reproduces the original plan exactly, including whatever
    #: ``activates_when`` decided the first time — those callables cannot be
    #: serialised, but their *outcome* can.
    only_units: frozenset[str] | None = None

    def work_units(self) -> list[tuple[Target, Dimension]]:
        pairs = [(t, d) for t in self.targets for d in self.dimensions if d.applies_to(t)]
        if self.only_units is None:
            return pairs
        return [(t, d) for t, d in pairs if self.unit_key(t, d) in self.only_units]

    def unit_key(self, target: Target, dimension: Dimension) -> str:
        return f"{target.key}::{dimension.key}"

    def as_dict(self) -> dict:
        """Fully serialisable form — enough to rebuild the spec for a resume.

        The prompts are included deliberately. Storing only dimension *names*
        meant a resumed run could rebuild built-in recipes and nothing else,
        which made resume useless for exactly the custom, long, expensive runs
        that most need it.
        """
        units = self.work_units()
        return {
            "topic": self.topic,
            "targets": [
                {"key": t.key, "label": t.label, "context": t.context} for t in self.targets
            ],
            "dimensions": [
                {
                    "key": d.key,
                    "prompt": d.prompt,
                    "why": d.why,
                    "grounded": d.grounded,
                    "max_tokens": d.max_tokens,
                    "temperature": d.temperature,
                    "output_keys": list(d.output_keys),
                }
                for d in self.dimensions
            ],
            "system_prompt": self.system_prompt,
            "output_contract": self.output_contract,
            "synthesis_prompt": self.synthesis_prompt,
            "findings_key": self.findings_key,
            "dedupe_fields": list(self.dedupe_fields),
            "url_fields": list(self.url_fields),
            "verifier_votes": self.verifier_votes,
            "verifier_lenses": list(self.verifier_lenses),
            "planned_units": [self.unit_key(t, d) for t, d in units],
            "work_units": len(units),
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> SwarmSpec:
        """Rebuild a spec saved by :meth:`as_dict`."""
        missing = [k for k in ("topic", "targets", "dimensions") if not data.get(k)]
        if missing:
            raise ValueError(f"saved spec is missing {missing}; cannot resume")
        if any("prompt" not in d for d in data["dimensions"]):
            raise ValueError(
                "saved spec predates prompt serialisation — re-run the original command; "
                "completed units are still reused, so nothing is paid for twice"
            )
        planned = data.get("planned_units")
        return cls(
            topic=data["topic"],
            targets=[Target(**t) for t in data["targets"]],
            dimensions=[
                Dimension(
                    key=d["key"],
                    prompt=d["prompt"],
                    why=d.get("why", ""),
                    grounded=d.get("grounded", True),
                    max_tokens=d.get("max_tokens", 8000),
                    temperature=d.get("temperature", 0.3),
                    output_keys=tuple(d.get("output_keys", ())),
                )
                for d in data["dimensions"]
            ],
            system_prompt=data.get("system_prompt", ""),
            output_contract=data.get("output_contract", "{}"),
            synthesis_prompt=data.get("synthesis_prompt"),
            findings_key=data.get("findings_key", "findings"),
            dedupe_fields=tuple(data.get("dedupe_fields", ("url", "name", "title"))),
            url_fields=tuple(data.get("url_fields", ("url",))),
            verifier_votes=data.get("verifier_votes", 0),
            verifier_lenses=tuple(
                data.get("verifier_lenses") or cls.__dataclass_fields__["verifier_lenses"].default
            ),
            notes=data.get("notes", ""),
            only_units=frozenset(planned) if planned else None,
        )
