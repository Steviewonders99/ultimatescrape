"""Markdown report rendering.

The report is written to be *auditable*, not impressive. Verification status,
corroboration counts, dead URLs, per-agent failures and the exact spend all
appear in it, because a research artifact whose provenance you cannot check is
indistinguishable from a well-written guess.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..swarm.orchestrator import SwarmResult

_VERDICT_MARK = {"supported": "✓", "refuted": "✗", "unverified": "?"}


def _fmt(value: Any, width: int = 60) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value[:3])
    text = str(value).replace("\n", " ").replace("|", "\\|").strip()
    return text[: width - 1] + "…" if len(text) > width else text


def _table(rows: Sequence[dict], columns: Sequence[tuple[str, str]]) -> str:
    if not rows:
        return "_No rows._\n"
    header = "| " + " | ".join(label for _, label in columns) + " |"
    divider = "|" + "|".join("---" for _ in columns) + "|"
    body = [
        "| " + " | ".join(_fmt(row.get(key)) for key, _ in columns) + " |" for row in rows
    ]
    return "\n".join([header, divider, *body]) + "\n"


def render(result: SwarmResult, *, top_n: int = 60) -> str:
    spec = result.spec
    stats = result.stats
    ledger = result.ledger

    findings = result.findings
    supported = [f for f in findings if f.get("_verdict") == "supported"]
    refuted = [f for f in findings if f.get("_verdict") == "refuted"]
    unverified = [f for f in findings if f.get("_verdict") not in ("supported", "refuted")]

    out: list[str] = []
    out.append(f"# {spec.get('topic', 'Research run')}\n")
    out.append(
        f"_Run `{result.run_id}` · {stats.get('units_completed', 0)}/{stats.get('units_total', 0)} "
        f"agents completed · {stats.get('elapsed_s', 0)}s · "
        f"${ledger.get('cost_usd', 0):.4f} spent_\n"
    )
    if stats.get("aborted"):
        out.append(
            "> **This run was cut short** by a budget ceiling or deadline. Findings below are "
            "partial. Resume with `uscrape resume " + result.run_id + "`.\n"
        )

    out.append("\n## Summary\n")
    out.append(
        _table(
            [
                {"metric": "Targets", "value": stats.get("targets")},
                {"metric": "Dimensions", "value": stats.get("dimensions")},
                {"metric": "Agents run", "value": stats.get("units_total")},
                {"metric": "Agents failed", "value": stats.get("units_failed")},
                {"metric": "Raw findings", "value": stats.get("findings_raw")},
                {"metric": "Unique findings", "value": stats.get("findings_unique")},
                {"metric": "Verified", "value": stats.get("findings_verified")},
                {"metric": "Refuted", "value": stats.get("findings_refuted")},
                {"metric": "Dead URLs", "value": stats.get("urls_dead")},
                {"metric": "Cost (USD)", "value": f"${ledger.get('cost_usd', 0):.4f}"},
                {
                    "metric": "Tokens (in/out)",
                    "value": f"{ledger.get('prompt_tokens', 0):,} / {ledger.get('completion_tokens', 0):,}",
                },
            ],
            [("metric", "Metric"), ("value", "Value")],
        )
    )

    if result.synthesis:
        out.append("\n## Synthesis\n")
        out.append(result.synthesis.strip() + "\n")

    columns = _infer_columns(findings)
    if supported:
        out.append(f"\n## Verified findings ({len(supported)})\n")
        out.append(_table(supported[:top_n], columns))
    if unverified:
        out.append(f"\n## Unverified findings ({len(unverified)})\n")
        out.append(
            "_Not adjudicated — either verification was disabled or every verifier failed. "
            "Treat as leads, not facts._\n\n"
        )
        out.append(_table(unverified[:top_n], columns))
    if refuted:
        out.append(f"\n## Refuted findings ({len(refuted)})\n")
        out.append(
            "_Kept deliberately. A refuted finding is evidence about the topic and about the "
            "swarm; deleting them hides both._\n\n"
        )
        out.append(
            _table(
                [{**f, "_why": "; ".join(f.get("_problems", [])[:2])} for f in refuted[:top_n]],
                [*columns[:3], ("_verdict_votes", "Votes"), ("_why", "Why refuted")],
            )
        )

    gaps = [
        {"agent": u.get("_meta", {}).get("target"), "dimension": u.get("_meta", {}).get("dimension"), "gap": u.get("gaps")}
        for u in result.units
        if u.get("gaps")
    ]
    if gaps:
        out.append(f"\n## Coverage gaps reported by agents ({len(gaps)})\n")
        out.append(
            "_What the swarm looked for and could not find. An unstated gap reads downstream "
            "as a finding of zero._\n\n"
        )
        out.append(
            _table(gaps[:40], [("agent", "Target"), ("dimension", "Dimension"), ("gap", "Gap")])
        )

    if result.errors:
        out.append(f"\n## Failures ({len(result.errors)})\n")
        out.append(
            _table(
                result.errors[:40],
                [("unit", "Unit"), ("stage", "Stage"), ("error", "Error")],
            )
        )

    out.append("\n## Method\n")
    out.append(
        f"Fan-out of {stats.get('targets', 0)} targets × {stats.get('dimensions', 0)} research "
        "dimensions, each a separate web-grounded agent with its own JSON contract. Results were "
        "deduplicated across agents (corroboration counted, not discarded), every claimed URL was "
        "liveness-checked deterministically, and each surviving finding was adjudicated by "
        f"{spec.get('verifier_votes', 0)} adversarial verifier(s) applying distinct lenses. "
        "Numbers are computed in code; the models supply judgement and prose only.\n"
    )
    by_model = ledger.get("by_model") or {}
    if by_model:
        out.append("\n### Spend by model\n")
        out.append(
            _table(
                [{"model": m, **v} for m, v in by_model.items()],
                [("model", "Model"), ("calls", "Calls"), ("prompt", "In"), ("completion", "Out"), ("cost_usd", "USD")],
            )
        )
    return "\n".join(out)


def _infer_columns(findings: Sequence[dict]) -> list[tuple[str, str]]:
    """Pick table columns from what the agents actually returned.

    The spec's output contract is user-defined, so hardcoding columns would only
    ever fit one kind of run.
    """
    preferred = [
        ("name", "Name"),
        ("title", "Title"),
        ("company", "Company"),
        ("country", "Country"),
        ("value", "Value"),
        ("url", "URL"),
        ("website", "Website"),
        ("linkedin", "LinkedIn"),
        ("summary", "Summary"),
        ("confidence", "Conf"),
    ]
    present = {k for f in findings[:50] for k in f if not k.startswith("_")}
    columns = [(k, label) for k, label in preferred if k in present]
    if len(columns) < 3:
        extra = [k for k in sorted(present) if k not in {c for c, _ in columns}]
        columns += [(k, k.replace("_", " ").title()) for k in extra[: 4 - len(columns)]]
    columns.append(("_corroborations", "Agents"))
    columns.append(("_verdict", "Verdict"))
    return columns
