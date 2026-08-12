"""Integration tests over real MCP connections.

These spawn `risk-mcp` and `pricing-mcp` as actual subprocesses and speak
the Model Context Protocol to them over stdio. Slower than the unit suite
by design - this is what proves the agent-to-agent claim is real rather
than a function call wearing a costume.
"""

from __future__ import annotations

import pytest

from mcp_uw_workbench.gateway import PRICING, RISK, MCPGateway
from mcp_uw_workbench.underwriting.orchestrator import run_quote

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_peers_advertise_expected_tools() -> None:
    """MCP capability discovery: each peer publishes its tool schema."""
    async with MCPGateway() as gw:
        tools = await gw.list_peer_tools()

    assert set(tools[RISK]) == {"score_risk", "pull_loss_history", "assess_property"}
    assert set(tools[PRICING]) == {
        "lookup_base_rate",
        "calculate_premium",
        "apply_modifiers",
    }


async def test_tool_call_round_trip_over_stdio() -> None:
    """A call crosses a process boundary and returns structured data."""
    async with MCPGateway() as gw:
        out = await gw.call(
            RISK, "score_risk", {"applicant_id": "APP-00003", "peril": "fire"}
        )

    assert out["applicant_id"] == "APP-00003"
    assert 0.0 <= out["score"] <= 100.0
    assert "extreme wildfire risk" in out["factors"]


async def test_peer_errors_propagate_as_data() -> None:
    """Unknown inputs return an error payload, not a transport failure."""
    async with MCPGateway() as gw:
        out = await gw.call(
            RISK, "score_risk", {"applicant_id": "APP-99999", "peril": "fire"}
        )
    assert "error" in out


async def test_end_to_end_quote_over_real_mcp() -> None:
    """Full supervisor -> peer -> peer flow with no in-process shortcuts."""
    quote = await run_quote(
        applicant_id="APP-00001",
        product="homeowners",
        state="FL",
        coverage_amount_usd=750_000.0,
        peril="hurricane",
    )

    assert quote["decision"] in {"quote", "refer", "decline"}
    assert quote["premium"]["final_premium_usd"] > 0
    # Every delegation below crossed an MCP boundary.
    for expected in (
        "risk.score_risk",
        "pricing.lookup_base_rate",
        "pricing.apply_modifiers",
    ):
        assert expected in quote["rationale"]


async def test_direct_and_mcp_gateways_agree() -> None:
    """The transport must not change the answer.

    Guards against the in-process test double drifting from real MCP
    behaviour - the failure mode that would make the unit suite lie.
    """
    from mcp_uw_workbench.gateway import DirectGateway

    args = {
        "applicant_id": "APP-00002",
        "product": "homeowners",
        "state": "TX",
        "coverage_amount_usd": 400_000.0,
        "peril": "wind",
    }

    direct = await run_quote(**args, gateway=DirectGateway())
    over_mcp = await run_quote(**args)

    assert direct["decision"] == over_mcp["decision"]
    assert (
        direct["premium"]["final_premium_usd"]
        == over_mcp["premium"]["final_premium_usd"]
    )
