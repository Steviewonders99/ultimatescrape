"""Daily USD FX table. frankfurter.app (ECB reference rates), keyless,
file-cached one day. A conversion is a claim: it always travels with as_of.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx

FRANKFURTER = "https://api.frankfurter.app/latest?from=USD"


@dataclass(frozen=True)
class FxTable:
    as_of: date
    to_usd: dict[str, float]  # currency code -> multiplier into USD

    def convert(self, amount: float, currency: str) -> float | None:
        rate = self.to_usd.get(currency.upper())
        return None if rate is None else amount * rate


async def load_fx(cache_path: Path) -> FxTable:
    if cache_path.exists():
        cached = json.loads(cache_path.read_text())
        if cached.get("fetched_on") == date.today().isoformat():
            return FxTable(
                as_of=date.fromisoformat(cached["as_of"]),
                to_usd=cached["to_usd"],
            )
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.get(FRANKFURTER)
        resp.raise_for_status()
        data = resp.json()
    # frankfurter returns USD->X; invert to X->USD.
    to_usd = {"USD": 1.0}
    for code, usd_to_x in data["rates"].items():
        if usd_to_x:
            to_usd[code.upper()] = 1.0 / usd_to_x
    table = FxTable(as_of=date.fromisoformat(data["date"]), to_usd=to_usd)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({
        "fetched_on": date.today().isoformat(),
        "as_of": table.as_of.isoformat(),
        "to_usd": table.to_usd,
    }))
    return table
