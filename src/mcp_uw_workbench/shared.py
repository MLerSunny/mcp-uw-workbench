"""Shared Pydantic types used by all three MCP servers.

Single source of truth for cross-server schemas. If a type changes here,
the orchestrator's contract with risk-mcp / pricing-mcp changes — caller
beware.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ----- Domain primitives -----------------------------------------------------

Peril = Literal[
    "fire",
    "wildfire",
    "wind",
    "water",
    "theft",
    "liability",
    "earthquake",
    "flood",
    "hurricane",
]
# Note: not every peril is ratable in every state. `wildfire` and
# `earthquake` appear in loss history but have no entry in the homeowners
# rate tables - `lookup_base_rate` returns a structured error for those,
# which the orchestrator surfaces rather than silently pricing at zero.

ProductLine = Literal["homeowners", "auto", "umbrella", "renters"]

UsState = Literal[
    "FL", "TX", "CA", "NY", "IL", "PA", "OH", "GA", "NC", "MI",
    "NJ", "VA", "WA", "AZ", "MA", "TN", "IN", "MO", "MD", "WI",
]


# ----- Applicant -------------------------------------------------------------

class Applicant(BaseModel):
    """Synthetic applicant. No real PII."""

    applicant_id: str = Field(..., description="Synthetic ID (e.g., 'APP-00042').")
    full_name: str = Field(..., description="Synthetic name.")
    age: int = Field(..., ge=18, le=120)
    state: UsState
    credit_band: Literal["A", "B", "C", "D"] = Field(
        ..., description="Insurance-score band (synthetic)."
    )
    prior_claims_count: int = Field(0, ge=0)
    occupation_class: Literal["I", "II", "III", "IV"] = "II"


# ----- Risk ------------------------------------------------------------------

class LossEvent(BaseModel):
    date: str  # ISO date
    peril: Peril
    paid_amount_usd: float
    closed: bool = True


class RiskScore(BaseModel):
    """Output of risk-mcp.score_risk."""

    applicant_id: str
    peril: Peril
    score: float = Field(..., ge=0.0, le=100.0, description="0=lowest risk, 100=highest.")
    factors: list[str] = Field(default_factory=list, description="Top contributing factors.")
    confidence: float = Field(..., ge=0.0, le=1.0)


class PropertyAssessment(BaseModel):
    """Output of risk-mcp.assess_property."""

    address: str
    coastal_distance_km: float
    flood_zone: str
    wildfire_risk: Literal["low", "moderate", "high", "extreme"]
    construction_year: int


# ----- Pricing ---------------------------------------------------------------

class BaseRate(BaseModel):
    state: UsState
    product: ProductLine
    peril: Peril
    rate_per_1000: float = Field(..., description="USD premium per $1000 of coverage.")


class PremiumBreakdown(BaseModel):
    base_premium_usd: float
    risk_loading_usd: float
    surcharges_usd: float
    credits_usd: float
    final_premium_usd: float
    components: dict[str, float] = Field(default_factory=dict)


# ----- Underwriting decision -------------------------------------------------

EligibilityOutcome = Literal["eligible", "declined", "refer_to_underwriter"]


class EligibilityCheck(BaseModel):
    applicant_id: str
    product: ProductLine
    state: UsState
    outcome: EligibilityOutcome
    reasons: list[str] = Field(default_factory=list)


class Quote(BaseModel):
    """Output of underwriting-mcp.generate_quote."""

    applicant_id: str
    product: ProductLine
    state: UsState
    eligibility: EligibilityCheck
    risk_score: RiskScore | None = None
    premium: PremiumBreakdown | None = None
    rationale: str = ""
    decision: Literal["quote", "decline", "refer"] = "refer"
