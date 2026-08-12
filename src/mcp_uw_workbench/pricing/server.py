"""pricing-mcp — rate-table lookup and premium calculation as an MCP server.

Exposed tools:
  * lookup_base_rate(state, product, peril) -> BaseRate
  * calculate_premium(coverage_amount_usd, base_rate_per_1000, risk_score)
       -> PremiumBreakdown
  * apply_modifiers(premium_breakdown, credit_band, prior_claims,
                    construction_year, coastal_within_5km) -> PremiumBreakdown
"""

from __future__ import annotations

from mcp.server import MCPServer

from mcp_uw_workbench.data_loader import load_rate_tables
from mcp_uw_workbench.shared import BaseRate, PremiumBreakdown

mcp = MCPServer("pricing-mcp")


# ----- Helpers ----------------------------------------------------------------

def _base_rate(state: str, product: str, peril: str) -> float | None:
    tables = load_rate_tables()
    return tables.get(product, {}).get(state, {}).get(peril)


# ----- Tools ------------------------------------------------------------------

@mcp.tool()
def lookup_base_rate(state: str, product: str, peril: str) -> dict:
    """Return the base rate per $1000 of coverage for (state, product, peril).

    Returns 0.0 with a note for combinations not on file (e.g., hurricane in CA).
    """
    rate = _base_rate(state, product, peril)
    if rate is None:
        return {
            "error": f"No rate on file for {product}/{state}/{peril}",
            "rate_per_1000": 0.0,
        }
    return BaseRate(
        state=state,  # type: ignore[arg-type]
        product=product,  # type: ignore[arg-type]
        peril=peril,  # type: ignore[arg-type]
        rate_per_1000=rate,
    ).model_dump()


@mcp.tool()
def calculate_premium(
    coverage_amount_usd: float,
    base_rate_per_1000: float,
    risk_score: float,
) -> dict:
    """Convert (coverage, rate, risk) into a base premium with risk loading.

    Args:
        coverage_amount_usd: Dwelling or liability limit.
        base_rate_per_1000: From `lookup_base_rate`.
        risk_score: 0-100 from risk-mcp.score_risk.
    """
    base_premium = (coverage_amount_usd / 1000.0) * base_rate_per_1000
    # Risk loading: a 50-score gives 0% load, 100-score gives 50% load.
    loading_pct = max(0.0, (risk_score - 50.0) / 100.0)
    risk_loading = base_premium * loading_pct

    return PremiumBreakdown(
        base_premium_usd=round(base_premium, 2),
        risk_loading_usd=round(risk_loading, 2),
        surcharges_usd=0.0,
        credits_usd=0.0,
        final_premium_usd=round(base_premium + risk_loading, 2),
        components={
            "coverage_amount_usd": coverage_amount_usd,
            "base_rate_per_1000": base_rate_per_1000,
            "risk_score": risk_score,
            "loading_pct": round(loading_pct, 3),
        },
    ).model_dump()


@mcp.tool()
def apply_modifiers(
    base_premium_usd: float,
    risk_loading_usd: float,
    credit_band: str = "B",
    prior_claims: int = 0,
    construction_year: int = 2000,
    coastal_within_5km: bool = False,
) -> dict:
    """Apply surcharges and credits to a base + risk-loaded premium."""
    tables = load_rate_tables()
    mods = tables.get("modifiers", {})

    band_mod = mods.get("credit_band", {}).get(credit_band, 0.0)
    pc_key = "3+" if prior_claims >= 3 else str(prior_claims)
    pc_mod = mods.get("prior_claims", {}).get(pc_key, 0.0)
    age_mod = mods.get("construction_year_pre_1990", 0.0) if construction_year < 1990 else 0.0
    coastal_mod = mods.get("coastal_within_5km", 0.0) if coastal_within_5km else 0.0

    sum_pct = band_mod + pc_mod + age_mod + coastal_mod
    pre_mod_total = base_premium_usd + risk_loading_usd
    adjustment = pre_mod_total * sum_pct
    # Both lines guard against -0.0, which renders as "$-0.00" in the quote
    # rationale. max() returns its *first* argument when the two compare equal,
    # so the 0.0 must lead; -min(adjustment, 0.0) would reintroduce the bug.
    surcharges = max(0.0, adjustment)
    credits = -adjustment if adjustment < 0.0 else 0.0

    final = pre_mod_total + adjustment

    return PremiumBreakdown(
        base_premium_usd=round(base_premium_usd, 2),
        risk_loading_usd=round(risk_loading_usd, 2),
        surcharges_usd=round(surcharges, 2),
        credits_usd=round(credits, 2),
        final_premium_usd=round(final, 2),
        components={
            "credit_band_mod": band_mod,
            "prior_claims_mod": pc_mod,
            "construction_age_mod": age_mod,
            "coastal_mod": coastal_mod,
            "sum_pct": round(sum_pct, 3),
        },
    ).model_dump()


def main() -> None:
    """Entry point. Speaks MCP over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
