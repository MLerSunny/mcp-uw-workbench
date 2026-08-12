"""underwriting-mcp - the supervisor agent.

Exposes applicant context and eligibility directly, and exposes
`generate_quote`, which runs the LangGraph orchestrator. That orchestrator
delegates risk scoring and pricing to `risk-mcp` and `pricing-mcp` over
real MCP connections.

So a client calling `generate_quote` triggers a chain:

    client --MCP--> underwriting-mcp --MCP--> risk-mcp
                                     --MCP--> pricing-mcp
"""

from __future__ import annotations

from mcp.server import MCPServer

from mcp_uw_workbench.data_loader import get_applicant
from mcp_uw_workbench.shared import Applicant
from mcp_uw_workbench.underwriting.orchestrator import _eligibility, run_quote

mcp = MCPServer("underwriting-mcp")


@mcp.tool()
def lookup_applicant(applicant_id: str) -> dict:
    """Return applicant context (demographics, credit band, prior-claims count)."""
    try:
        raw = get_applicant(applicant_id)
    except KeyError:
        return {"error": f"Unknown applicant_id: {applicant_id}"}

    return Applicant(
        applicant_id=raw["applicant_id"],
        full_name=raw["full_name"],
        age=raw["age"],
        state=raw["state"],
        credit_band=raw["credit_band"],
        prior_claims_count=raw["prior_claims_count"],
        occupation_class=raw.get("occupation_class", "II"),
    ).model_dump()


@mcp.tool()
def check_eligibility(
    applicant_id: str,
    product: str,
    state: str,
    coverage_amount_usd: float | None = None,
) -> dict:
    """Apply underwriting eligibility rules for a product and state.

    Returns one of: eligible, refer_to_underwriter, declined - with reasons.

    Args:
        applicant_id: Synthetic applicant identifier.
        product: Product line, e.g. "homeowners".
        state: Two-letter state code.
        coverage_amount_usd: Requested limit. Omit to skip the capacity
            check; supply it to have coverage above the carrier's filed
            capacity declined outright.
    """
    try:
        applicant = get_applicant(applicant_id)
    except KeyError:
        return {"error": f"Unknown applicant_id: {applicant_id}"}

    return _eligibility(applicant, product, state, coverage_amount_usd).model_dump()


@mcp.tool()
async def generate_quote(
    applicant_id: str,
    product: str = "homeowners",
    state: str = "FL",
    coverage_amount_usd: float = 500_000.0,
    peril: str = "hurricane",
) -> dict:
    """Produce a complete underwriting decision for an applicant.

    Orchestrates across peer agents: risk-mcp scores the peril and pulls
    loss history; pricing-mcp looks up the base rate, computes premium, and
    applies modifiers. Returns eligibility, risk score, premium breakdown,
    a human-readable rationale, and the list of delegated tool calls.
    """
    try:
        return await run_quote(
            applicant_id=applicant_id,
            product=product,
            state=state,
            coverage_amount_usd=coverage_amount_usd,
            peril=peril,
        )
    except KeyError as e:
        return {"error": f"Lookup failed: {e}"}
    except Exception as e:  # noqa: BLE001 - surface orchestrator faults to the client
        return {"error": f"Orchestrator error: {type(e).__name__}: {e}"}


def main() -> None:
    """Entry point. Speaks MCP over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
