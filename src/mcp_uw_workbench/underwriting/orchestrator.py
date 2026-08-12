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

The declined branch is not decorative: coverage above the carrier's filed
capacity is refused outright, and refusing it *before* the risk and pricing
delegations is the point - there is no reason to spend two peer round-trips
scoring a risk we have no capacity to write.
"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from mcp_uw_workbench.data_loader import get_applicant, load_underwriting_rules
from mcp_uw_workbench.gateway import PRICING, RISK, PeerUnavailable, ToolGateway
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
    loss_history: dict[str, Any]
    base_rate: dict[str, Any]
    premium: dict[str, Any]
    quote: dict[str, Any]
    delegations: list[str]
    pricing_gap: str
    peer_failure: str


def _capacity_limit(product: str, state: str) -> float | None:
    """Maximum coverage the carrier will write for this product and state."""
    rules = load_underwriting_rules()
    limits = rules.get("capacity_limits_usd", {})
    by_state = limits.get(product)
    if by_state is None:
        return rules.get("default_capacity_usd")
    return by_state.get(state, rules.get("default_capacity_usd"))


def _eligibility(
    applicant: dict[str, Any],
    product: str,
    state: str,
    coverage_amount_usd: float | None = None,
) -> EligibilityCheck:
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

    # Capacity is a hard stop, evaluated last so it outranks any referral
    # above it. No amount of underwriter review makes a risk writable when
    # the carrier has no capacity for it, so this declines rather than refers.
    if coverage_amount_usd is not None:
        limit = _capacity_limit(product, state)
        if limit is not None and coverage_amount_usd > limit:
            reasons.append(
                f"coverage ${coverage_amount_usd:,.0f} exceeds "
                f"${limit:,.0f} capacity for {product}/{state}"
            )
            outcome = "declined"

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
            state["applicant"],
            state["product"],
            state["state"],
            state.get("coverage_amount_usd"),
        ).model_dump()
        return state

    async def risk(state: QuoteState) -> QuoteState:
        try:
            # --- A2A delegation: supervisor -> risk agent ---
            state["risk"] = await gateway.call(
                RISK, "score_risk",
                {"applicant_id": state["applicant_id"], "peril": state["peril"]},
            )
            state["delegations"].append("risk.score_risk")

            state["loss_history"] = await gateway.call(
                RISK, "pull_loss_history", {"applicant_id": state["applicant_id"]}
            )
            state["delegations"].append("risk.pull_loss_history")
        except PeerUnavailable as exc:
            state["peer_failure"] = str(exc)
        return state

    async def pricing(state: QuoteState) -> QuoteState:
        # The risk peer already failed. Pricing depends on its score, so
        # calling out again would only produce a second failure to report.
        if state.get("peer_failure"):
            return state

        try:
            # --- A2A delegation: supervisor -> pricing agent ---
            base_rate = await gateway.call(
                PRICING, "lookup_base_rate",
                {
                    "state": state["state"],
                    "product": state["product"],
                    "peril": state["peril"],
                },
            )
        except PeerUnavailable as exc:
            state["peer_failure"] = str(exc)
            return state

        state["base_rate"] = base_rate
        state["delegations"].append("pricing.lookup_base_rate")

        # No filed rate for this state/product/peril. Pricing downstream would
        # happily return $0, which reads as a valid free policy. Flag it so
        # compile_quote forces a referral instead of publishing the zero.
        if "error" in base_rate or not base_rate.get("rate_per_1000"):
            state["pricing_gap"] = (
                f"No filed rate for {state['product']}/{state['state']}/{state['peril']}"
            )

        try:
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
        except PeerUnavailable as exc:
            state["peer_failure"] = str(exc)
            return state

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

        # Aggregate severity, not just claim frequency. Two large losses can
        # outweigh three small ones, and the count-based eligibility rule
        # above cannot see that - only the loss-history delegation can.
        events = (state.get("loss_history") or {}).get("events") or []
        total_paid = sum(float(e.get("paid_amount_usd", 0.0)) for e in events)
        loss_threshold = load_underwriting_rules().get("aggregate_loss_referral_usd")
        loss_referral = loss_threshold is not None and total_paid >= loss_threshold
        if loss_referral and decision == "quote":
            decision = "refer"

        # A peer we could not reach must never fall through to an automatic
        # quote. Fail closed: refer to a human instead.
        peer_failure = state.get("peer_failure")
        if peer_failure and decision == "quote":
            decision = "refer"

        parts = [f"Eligibility: {eligibility['outcome']}"]
        if eligibility["reasons"]:
            parts.append("Reasons: " + "; ".join(eligibility["reasons"]))
        if pricing_gap:
            parts.append(f"Pricing gap: {pricing_gap} - referred, not auto-priced")
        if peer_failure:
            parts.append(f"Peer agent unavailable: {peer_failure} - referred, not auto-quoted")
        if events:
            parts.append(
                f"Loss history: {len(events)} event(s), ${total_paid:,.2f} paid"
            )
        if loss_referral:
            parts.append(
                f"Aggregate paid losses ${total_paid:,.2f} meet the "
                f"${loss_threshold:,.0f} referral threshold"
            )
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
            risk_model = RiskScore(**risk_data)

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
        """Skip both peer agents when the risk is declined outright.

        Worth measuring: a declined applicant costs zero peer round-trips,
        which the eval harness asserts via an empty expected_delegations.
        """
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
