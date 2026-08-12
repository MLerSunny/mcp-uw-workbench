"""risk-mcp — risk scoring and loss-history retrieval as an MCP server.

Exposed tools:
  * score_risk(applicant_id, peril) -> RiskScore
  * pull_loss_history(applicant_id) -> list[LossEvent]
  * assess_property(applicant_id) -> PropertyAssessment

All scoring logic is a simple deterministic function over synthetic data.
A real implementation would call an ML model registry (e.g., MLflow).
"""

from __future__ import annotations

from mcp.server import MCPServer

from mcp_uw_workbench.data_loader import get_applicant
from mcp_uw_workbench.shared import (
    LossEvent,
    Peril,
    PropertyAssessment,
    RiskScore,
)

mcp = MCPServer("risk-mcp")


# ----- Scoring heuristic ------------------------------------------------------

def _score(applicant: dict, peril: Peril) -> tuple[float, list[str]]:
    """Toy scoring function. Replace with model inference in v0.2.

    Returns (score 0-100, factors).
    """
    score = 25.0  # base score
    factors: list[str] = []

    # Prior claims
    pc = applicant.get("prior_claims_count", 0)
    if pc >= 3:
        score += 30
        factors.append(f"{pc} prior claims (severe)")
    elif pc == 2:
        score += 18
        factors.append("2 prior claims (elevated)")
    elif pc == 1:
        score += 8
        factors.append("1 prior claim (minor)")

    # Credit band
    band_loading = {"A": -5, "B": 0, "C": 8, "D": 18}
    band = applicant.get("credit_band", "B")
    score += band_loading.get(band, 0)
    factors.append(f"credit band {band}")

    # Property risk for property perils
    prop = applicant.get("property", {})
    if peril == "hurricane" and prop.get("coastal_distance_km", 999) < 5:
        score += 22
        factors.append("coastal property within 5km")
    if peril == "wildfire" or peril == "fire":
        wf = prop.get("wildfire_risk", "low")
        if wf == "extreme":
            score += 28
            factors.append("extreme wildfire risk")
        elif wf == "high":
            score += 16
            factors.append("high wildfire risk")
    if peril in ("water", "flood", "hurricane") and prop.get("flood_zone") == "AE":
        score += 12
        factors.append("flood zone AE")

    return min(score, 99.5), factors


# ----- Tools ------------------------------------------------------------------

@mcp.tool()
def score_risk(applicant_id: str, peril: str) -> dict:
    """Score peril-specific risk for an applicant (0-100, higher = more risk).

    Args:
        applicant_id: Synthetic applicant identifier (e.g., "APP-00001").
        peril: One of fire, wind, water, theft, liability, earthquake,
               flood, hurricane.
    """
    try:
        applicant = get_applicant(applicant_id)
    except KeyError:
        return {"error": f"Unknown applicant_id: {applicant_id}"}

    score_val, factors = _score(applicant, peril)  # type: ignore[arg-type]
    confidence = 0.85 if applicant.get("prior_claims_count", 0) >= 2 else 0.72

    return RiskScore(
        applicant_id=applicant_id,
        peril=peril,  # type: ignore[arg-type]
        score=score_val,
        factors=factors,
        confidence=confidence,
    ).model_dump()


@mcp.tool()
def pull_loss_history(applicant_id: str) -> dict:
    """Return prior loss events for an applicant.

    In v0.2 this would call a CLUE-equivalent service. For now: synthetic data.
    """
    try:
        applicant = get_applicant(applicant_id)
    except KeyError:
        return {"error": f"Unknown applicant_id: {applicant_id}"}

    events = [LossEvent(**e).model_dump() for e in applicant.get("loss_history", [])]
    return {"applicant_id": applicant_id, "events": events, "count": len(events)}


@mcp.tool()
def assess_property(applicant_id: str) -> dict:
    """Return geo/structural risk attributes for an applicant's property."""
    try:
        applicant = get_applicant(applicant_id)
    except KeyError:
        return {"error": f"Unknown applicant_id: {applicant_id}"}

    prop = applicant.get("property")
    if not prop:
        return {"error": "No property on file for applicant."}

    return PropertyAssessment(**prop).model_dump()


def main() -> None:
    """Entry point (console_script). Speaks MCP over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
