"""UltimateScrape — swarm research and scraping infrastructure.

Three layers that compose:

  fetch/sources  getting bytes: polite HTTP, headless browser, official APIs
  channels       routing a URL to the right access path, with tiered fallback
  swarm          fanning out many small agents, then merging and verifying them

The design rule that matters most: LLMs produce *mappings, judgements, and prose*.
Every number, every dedupe, and every URL check is computed deterministically in
Python and asserted. That split is what makes a 300-agent run trustworthy rather
than merely large.
"""

from .config import Settings, settings
from .llm.budget import Ledger
from .llm.client import KimiClient
from .swarm.orchestrator import Swarm, SwarmResult
from .swarm.spec import Dimension, SwarmSpec, Target

__version__ = "0.1.0"

__all__ = [
    "Dimension",
    "KimiClient",
    "Ledger",
    "Settings",
    "Swarm",
    "SwarmResult",
    "SwarmSpec",
    "Target",
    "__version__",
    "settings",
]
