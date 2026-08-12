"""End-to-end demo: quotes generated over real MCP connections.

Spawns `risk-mcp` and `pricing-mcp` as subprocesses, opens MCP sessions to
both, and runs several applicants through the orchestrator. Every risk and
pricing figure below arrived over the wire from a separate process.

    python examples/01_quote_flow.py
"""

from __future__ import annotations

import anyio

from mcp_uw_workbench.gateway import MCPGateway
from mcp_uw_workbench.underwriting.orchestrator import build_graph

CASES = [
    ("APP-00004", "homeowners", "NY", 350_000.0, "water", "clean applicant, low-risk peril"),
    ("APP-00001", "homeowners", "FL", 750_000.0, "hurricane", "coastal FL within 1km"),
    ("APP-00003", "homeowners", "CA", 500_000.0, "fire", "3 prior claims, extreme wildfire"),
    ("APP-00004", "homeowners", "NY", 350_000.0, "earthquake", "no filed rate for peril"),
]


async def main() -> None:
    async with MCPGateway() as gateway:
        discovered = await gateway.list_peer_tools()
        print("Connected to peer agents over MCP (stdio):")
        for peer, tools in discovered.items():
            print(f"  {peer + '-mcp':<16} {', '.join(tools)}")

        graph = build_graph(gateway)

        for applicant, product, state, coverage, peril, note in CASES:
            result = await graph.ainvoke(
                {
                    "applicant_id": applicant,
                    "product": product,
                    "state": state,
                    "coverage_amount_usd": coverage,
                    "peril": peril,
                    "delegations": [],
                }
            )
            quote = result["quote"]
            premium = quote.get("premium") or {}

            print("\n" + "=" * 72)
            print(f"{applicant} · {state} · {peril} · ${coverage:,.0f}   ({note})")
            print("-" * 72)
            print(f"  decision : {quote['decision'].upper()}")
            if premium.get("final_premium_usd"):
                print(f"  premium  : ${premium['final_premium_usd']:,.2f}")
            for line in quote["rationale"].split(" | "):
                print(f"  {line}")


if __name__ == "__main__":
    anyio.run(main)
