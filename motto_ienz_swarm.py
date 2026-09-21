import asyncio
from ultimatescrape import Swarm
from ultimatescrape.swarm.spec import SwarmSpec, Target, Dimension
from ultimatescrape.swarm.prompts import RESEARCH_SYSTEM
from ultimatescrape.store.report import render

spec = SwarmSpec(
    topic=("What messaging, values and cultural codes make advertising land with 20-35 year olds "
           "with close friend groups in Ireland and New Zealand, for a paid group audio-recording "
           "project, given strong local skepticism toward AI"),
    targets=[Target.of("Ireland"), Target.of("New Zealand")],
    dimensions=[
        Dimension("ai_sentiment",
            "What do surveys from 2024-2026 say about public attitudes toward AI in {label}, "
            "especially distrust or skepticism among adults under 35? Name each survey, its year, "
            "and the headline percentages.", max_tokens=6000),
        Dimension("youth_economics",
            "What are the dominant financial pressures on 20-35 year olds in {label} in 2025-2026 "
            "(cost of living, rent/housing, emigration or moving away)? Cite recent statistics with sources.",
            max_tokens=6000),
        Dimension("friendship_culture",
            "How do 20-35 year olds in {label} spend time with close friends: typical settings, rituals, "
            "and the slang or phrases they actually use for hanging out and talking (2023-2026 sources)?",
            max_tokens=6000),
        Dimension("ad_resonance",
            "Which recent (2023-2026) advertising campaigns or brand voices have strongly resonated with "
            "young adults in {label}, and what tone do they share (humour, self-deprecation, anti-hype, "
            "local in-jokes)? Give named campaigns/brands with evidence of resonance.", max_tokens=6000),
        Dimension("side_income",
            "How are side gigs and paid tasks perceived by young adults in {label}: what makes a paid "
            "offer feel legitimate versus scammy, and what proportion have side income (2024-2026 data)?",
            max_tokens=6000),
        Dimension("tech_flashpoints",
            "What specific technology or data controversies are currently salient in {label} that an "
            "advertisement about recording group conversations for technology could accidentally trigger "
            "(for example data centres, data sovereignty, voice cloning scams)? Cite reporting.",
            max_tokens=6000),
    ],
    system_prompt=RESEARCH_SYSTEM,
    output_contract='{"findings":[{"name":"","summary":"","url":"","confidence":""}],"gaps":""}',
    dedupe_fields=("url", "name"),
    url_fields=("url",),
    verifier_votes=3,
)

async def main():
    async with Swarm() as swarm:
        result = await swarm.run(spec)
    print(render(result))

asyncio.run(main())
