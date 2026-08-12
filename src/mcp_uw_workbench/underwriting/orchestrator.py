"""LangGraph orchestrator - the supervisor agent.

This is where agent-to-agent delegation happens. The graph owns local
concerns (applicant context, eligibility rules, quote compilation) and
delegates risk scoring and pricing to peer agents through a `ToolGateway`.

In production that gateway is `MCPGateway`, which means every delegation
below is a real MCP `call_tool` over stdio to a separate OS process. The
orchestrator holds no shared memory or database with its peers - the only
contract is the MCP tool schema.

Graph shape:

    START -> lookup_applicant -> check_eligibility -+-> risk -> pricing -+-> compile_quote -> END
                                                    |                    |
                                                    +--------------------+
                                                     (declined: skip both)
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from mcp_uw_workbench.data_loader import get_applicant
from mcp_uw_workbench.gateway import PRICING, RISK, ToolGateway
from mcp_uw_workbench.shared import (
    EligibilityCheck,
    PremiumBreakdown,
    Quote,
    RiskScore,
)


class QuoteState(TypedDict, total=False):
    # Inputs
    applicant_id: str
    product: str
    state: str
    coverage_amount_usd: float
    peril: str

    # Accumulated
    applicant: dict[str, Any]
    eligibility: dict[str, Any]
    risk: dict[str, Any]
    base_rate: dict[str, Any]
    premium: dict[str, Any]
    quote: dict[str, Any]
    delegations: list[str]
    pricing_gap: str


def _eligibility(applicant: dict[str, Any], product: str, state: str) -> EligibilityCheck:
    """Underwriting eligibility rules. Local to the supervisor - these are
    company policy, not something a peer agent should own."""
    reasons: list[str] = []
    outcome = "eligible"

    if applicant.get("prior_claims_count", 0) >= 3:
        reasons.append("3+ prior claims in 5 years")
        outcome = "refer_to_underwriter"

    if product == "homeowners" and state == "FL":
        prop = applicant.get("property", {})
        if prop.get("coastal_distance_km", 999) < 1.0:
            reasons.append("coastal property within 1km")
            outcome = "refer_to_underwriter"

    return EligibilityCheck(
        applicant_id=applicant["applicant_id"],
        product=product,  # type: ignore[arg-type]
        state=state,  # type: ignore[arg-type]
        outcome=outcome,  # type: ignore[arg-type]
        reasons=reasons,
    )


def build_graph(gateway: ToolGateway) -> Any:
    """Compile a graph bound to a specific gateway.

    The gateway is closed over rather than threaded through state so that
    graph state stays JSON-serialisable (checkpointing, replay, tracing).
    """

    async def lookup_applicant(state: QuoteState) -> QuoteState:
        state["applicant"] = get_applicant(state["applicant_id"])
        state.setdefault("delegations", [])
        return state

    async def check_eligibility(state: QuoteState) -> QuoteState:
        state["eligibility"] = _eligibility(
            state["applicant"], state["product"], state["state"]
        ).model_dump()
        return state

    async def risk(state: QuoteState) -> QuoteState:
        # --- A2A delegation: supervisor -> risk agent ---
        state["risk"] = await gateway.call(
            RISK, "score_risk",
            {"applicant_id": state["applicant_id"], "peril": state["peril"]},
        )
        state["delegations"].append("risk.score_risk")

        history = await gateway.call(
            RISK, "pull_loss_history", {"applicant_id": state["applicant_id"]}
        )
        state["delegations"].append("risk.pull_loss_history")
        state["risk"]["loss_events"] = history.get("count", 0)
        return state

    async def pricing(state: QuoteState) -> QuoteState:
        # --- A2A delegation: supervisor -> pricing agent ---
        base_rate = await gateway.call(
            PRICING, "lookup_base_rate",
            {
                "state": state["state"],
                "product": state["product"],
                "peril": state["peril"],
            },
        )
        state["base_rate"] = base_rate
        state["delegations"].append("pricing.lookup_base_rate")

        # No filed rate for this state/product/peril. Pricing downstream would
        # happily return $0, which reads as a valid free policy. Flag it so
        # compile_quote forces a referral instead of publishing the zero.
        if "error" in base_rate or not base_rate.get("rate_per_1000"):
            state["pricing_gap"] = (
                f"No filed rate for {state['product']}/{state['state']}/{state['peril']}"
            )

        premium = await gateway.call(
            PRICING, "calculate_premium",
            {
                "coverage_amount_usd": state["coverage_amount_usd"],
                "base_rate_per_1000": base_rate.get("rate_per_1000", 0.0),
                "risk_score": state["risk"].get("score", 50.0),
            },
        )
        state["delegations"].append("pricing.calculate_premium")

        prop = state["applicant"].get("property", {})
        premium = await gateway.call(
            PRICING, "apply_modifiers",
            {
                "base_premium_usd": premium["base_premium_usd"],
                "risk_loading_usd": premium["risk_loading_usd"],
                "credit_band": state["applicant"].get("credit_band", "B"),
                "prior_claims": state["applicant"].get("prior_claims_count", 0),
                "construction_year": prop.get("construction_year", 2000),
                "coastal_within_5km": prop.get("coastal_distance_km", 999) < 5.0,
            },
        )
        state["premium"] = premium
        state["delegations"].append("pricing.apply_modifiers")
        return state

    async def compile_quote(state: QuoteState) -> QuoteState:
        eligibility = state["eligibility"]
        risk_data = state.get("risk")
        premium = state.get("premium")

        decision = {
            "declined": "decline",
            "refer_to_underwriter": "refer",
        }.get(eligibility["outcome"], "quote")

        # A missing filed rate is never auto-quotable, regardless of eligibility.
        pricing_gap = state.get("pricing_gap")
        if pricing_gap and decision == "quote":
            decision = "refer"

        parts = [f"Eligibility: {eligibility['outcome']}"]
        if eligibility["reasons"]:
            parts.append("Reasons: " + "; ".join(eligibility["reasons"]))
        if pricing_gap:
            parts.append(f"Pricing gap: {pricing_gap} - referred, not auto-priced")
        if risk_data and "score" in risk_data:
            parts.append(
                f"Risk score {risk_data['score']:.1f} "
                f"(confidence {risk_data.get('confidence', 0):.0%}); "
                "factors: " + ", ".join(risk_data.get("factors") or ["n/a"])
            )
        if premium:
            parts.append(
                f"Final premium ${premium['final_premium_usd']:,.2f} "
                f"(base ${premium['base_premium_usd']:,.2f}, "
                f"risk loading ${premium['risk_loading_usd']:,.2f}, "
                f"surcharges ${premium['surcharges_usd']:,.2f}, "
                f"credits ${premium['credits_usd']:,.2f})"
            )
        parts.append("Delegated to: " + ", ".join(state.get("delegations") or ["none"]))

        risk_model = None
        if risk_data and "score" in risk_data:
            risk_model = RiskScore(
                **{k: v for k, v in risk_data.items() if k != "loss_events"}
            )

        state["quote"] = Quote(
            applicant_id=state["applicant_id"],
            product=state["product"],  # type: ignore[arg-type]
            state=state["state"],  # type: ignore[arg-type]
            eligibility=EligibilityCheck(**eligibility),
            risk_score=risk_model,
            premium=PremiumBreakdown(**premium) if premium else None,
            rationale=" | ".join(parts),
            decision=decision,  # type: ignore[arg-type]
        ).model_dump()
        return state

    def route(state: QuoteState) -> str:
        return "compile_quote" if state["eligibility"]["outcome"] == "declined" else "risk"

    graph: StateGraph = StateGraph(QuoteState)
    graph.add_node("lookup_applicant", lookup_applicant)
    graph.add_node("check_eligibility", check_eligibility)
    graph.add_node("risk", risk)
    graph.add_node("pricing", pricing)
    graph.add_node("compile_quote", compile_quote)

    graph.add_edge(START, "lookup_applicant")
    graph.add_edge("lookup_applicant", "check_eligibility")
    graph.add_conditional_edges(
        "check_eligibility", route,
        {"risk": "risk", "compile_quote": "compile_quote"},
    )
    graph.add_edge("risk", "pricing")
    graph.add_edge("pricing", "compile_quote")
    graph.add_edge("compile_quote", END)

    return graph.compile()


async def run_quote(
    applicant_id: str,
    product: str = "homeowners",
    state: str = "FL",
    coverage_amount_usd: float = 500_000.0,
    peril: str = "hurricane",
    gateway: ToolGateway | None = None,
) -> dict[str, Any]:
    """Generate a quote.

    With no gateway supplied, opens a real `MCPGateway` - spawning the risk
    and pricing agents as subprocesses and speaking MCP to them. Tests pass
    a `DirectGateway` to skip subprocess cost.
    """
    initial: QuoteState = {
        "applicant_id": applicant_id,
        "product": product,
        "state": state,
        "coverage_amount_usd": coverage_amount_usd,
        "peril": peril,
        "delegations": [],
    }

    if gateway is not None:
        result = await build_graph(gateway).ainvoke(initial)
        return result["quote"]

    from mcp_uw_workbench.gateway import MCPGateway

    async with MCPGateway() as gw:
        result = await build_graph(gw).ainvoke(initial)
        return result["quote"]
