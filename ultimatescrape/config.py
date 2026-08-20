"""Central configuration. Every tunable is an env var with a sane default.

Load order for secrets, first hit wins:
  1. process environment
  2. ./.env in the current working directory
  3. any path listed in USCRAPE_ENV_FALLBACKS

The fallback list lets you point at an existing .env elsewhere on the machine
instead of copying secrets into a second file. Separate entries with the
platform path separator — ``;`` on Windows, ``:`` elsewhere — which is what
``os.pathsep`` gives you. Splitting on a literal colon would break every Windows
path at its drive letter.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

#: Optional extra .env locations, tried in order. Override with
#: USCRAPE_ENV_FALLBACKS. Missing paths are skipped silently.
_DEFAULT_FALLBACKS: tuple[str, ...] = ()


def _load_env() -> None:
    load_dotenv(Path.cwd() / ".env", override=False)
    # Also the checkout's own .env, so `uscrape` behaves the same from any cwd
    # (an editable install launched outside the repo used to silently lose
    # every key and extension configured there).
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
    raw = os.getenv("USCRAPE_ENV_FALLBACKS")
    paths = raw.split(os.pathsep) if raw else _DEFAULT_FALLBACKS
    for p in paths:
        if not p.strip():
            continue
        expanded = Path(p.strip()).expanduser()
        if expanded.is_file():
            load_dotenv(expanded, override=False)


_load_env()


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def _env_i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _env_list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in _env(name, default).split(",") if x.strip()]


# Cost per million tokens, USD. Used for the run budget meter. These drift, so
# treat them as estimates for guardrails, not for invoicing.
PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "moonshotai/kimi-k2.6": (0.60, 2.50),
    "moonshotai/kimi-k2.5": (0.60, 2.50),
    "moonshotai/kimi-k3": (3.00, 15.00),
    "moonshotai/kimi-k2.7-code": (0.71, 3.50),
    "moonshotai/kimi-k2": (0.60, 2.50),
    "qwen/qwen3.8-max": (2.00, 6.00),
    "qwen/qwen3.8-2.4t-a95b": (2.00, 6.00),
    "qwen/qwen3.8-27b": (0.45, 3.20),
    "google/gemma-4-31b-it": (0.10, 0.30),
    "anthropic/claude-haiku-4.5": (0.80, 4.00),
    "anthropic/claude-sonnet-4.6": (3.00, 15.00),
}
FALLBACK_PRICE = (1.00, 5.00)


@dataclass(frozen=True)
class Settings:
    # LLM
    llm_base_url: str = field(
        default_factory=lambda: _env("USCRAPE_LLM_BASE_URL", "https://openrouter.ai/api/v1")
    )
    # Some setups store an OpenRouter key under KIMI_API_KEY; accept both spellings.
    api_key: str = field(
        default_factory=lambda: _env("OPENROUTER_API_KEY")
        or _env("KIMI_API_KEY")
        or _env("MOONSHOT_API_KEY")
    )
    models: list[str] = field(
        default_factory=lambda: _env_list(
            "USCRAPE_MODELS",
            "moonshotai/kimi-k2.6,moonshotai/kimi-k2.5,google/gemma-4-31b-it",
        )
    )
    referer: str = field(default_factory=lambda: _env("USCRAPE_REFERER", "https://example.com"))
    app_title: str = field(default_factory=lambda: _env("USCRAPE_TITLE", "UltimateScrape"))

    # Spend
    max_run_cost_usd: float = field(
        default_factory=lambda: _env_f("USCRAPE_MAX_RUN_COST_USD", 25.0)
    )
    min_credits_usd: float = field(default_factory=lambda: _env_f("USCRAPE_MIN_CREDITS_USD", 5.0))

    # Concurrency and deadlines
    llm_concurrency: int = field(default_factory=lambda: _env_i("USCRAPE_LLM_CONCURRENCY", 12))
    fetch_concurrency: int = field(default_factory=lambda: _env_i("USCRAPE_FETCH_CONCURRENCY", 20))
    per_host_concurrency: int = field(
        default_factory=lambda: _env_i("USCRAPE_PER_HOST_CONCURRENCY", 4)
    )
    per_host_delay_s: float = field(default_factory=lambda: _env_f("USCRAPE_PER_HOST_DELAY_S", 1.0))
    agent_timeout_s: float = field(default_factory=lambda: _env_f("USCRAPE_AGENT_TIMEOUT_S", 300))
    run_deadline_s: float = field(default_factory=lambda: _env_f("USCRAPE_RUN_DEADLINE_S", 3600))
    fetch_timeout_s: float = field(default_factory=lambda: _env_f("USCRAPE_FETCH_TIMEOUT_S", 20))
    max_response_bytes: int = field(
        default_factory=lambda: _env_i("USCRAPE_MAX_RESPONSE_BYTES", 5_000_000)
    )

    # Retry
    max_attempts: int = field(default_factory=lambda: _env_i("USCRAPE_MAX_ATTEMPTS", 3))

    # Search
    search_max_results: int = field(default_factory=lambda: _env_i("WEB_SEARCH_MAX_RESULTS", 8))
    search_context_size: str = field(
        default_factory=lambda: _env("WEB_SEARCH_CONTEXT_SIZE", "low")
    )

    # Storage
    runs_dir: Path = field(
        default_factory=lambda: Path(_env("USCRAPE_RUNS_DIR", "runs")).expanduser()
    )

    # Robots / politeness
    respect_robots: bool = field(
        default_factory=lambda: _env("USCRAPE_RESPECT_ROBOTS", "true").lower() != "false"
    )
    user_agent: str = field(
        default_factory=lambda: _env(
            "USCRAPE_USER_AGENT",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 UltimateScrape/0.1",
        )
    )

    # LinkedIn
    linkedin_tiers: list[str] = field(
        default_factory=lambda: _env_list("USCRAPE_LINKEDIN_TIERS", "vendor,mcp,parser,jina")
    )

    def price(self, model: str) -> tuple[float, float]:
        return PRICE_PER_MTOK.get(model, FALLBACK_PRICE)

    @property
    def is_openrouter(self) -> bool:
        return "openrouter.ai" in self.llm_base_url


settings = Settings()


def nim_key_pool() -> list[str]:
    """Every configured NVIDIA NIM key, deduped.

    NIM rate limits are per key, so the strategy here is
    to multiply keys rather than throttle. Numbered keys NIM_KEY_1..NIM_KEY_20
    plus a CSV overflow var are all folded into one pool.
    """
    keys: list[str] = []
    for name in ("NVIDIA_NIM_API_KEY", "NVIDIA_NIM_VQA_KEY"):
        if v := _env(name):
            keys.append(v)
    keys.extend(_env_list("NIM_EXTRA_KEYS"))
    for i in range(1, 21):
        if v := _env(f"NIM_KEY_{i}"):
            keys.append(v)
    seen: set[str] = set()
    return [k for k in keys if not (k in seen or seen.add(k))]


def api_key_for(name: str) -> str:
    """Read a data-source API key by env var name. Empty string when absent."""
    return _env(name)
