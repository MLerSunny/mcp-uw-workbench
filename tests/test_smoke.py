"""Unit tests for peer-agent tools and orchestrator logic.

These run against `DirectGateway` (in-process) so the suite stays fast.
Real cross-process MCP behaviour is covered in `test_mcp_integration.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from mcp_uw_workbench.gateway import DirectGateway, PeerUnavailable
from mcp_uw_workbench.pricing.server import (
    apply_modifiers,
    calculate_premium,
    lookup_base_rate,
)
from mcp_uw_workbench.risk.server import pull_loss_history, score_risk
from mcp_uw_workbench.underwriting.orchestrator import run_quote
from mcp_uw_workbench.underwriting.server import check_eligibility, lookup_applicant

# --------------------------------------------------------------------------
# risk-mcp
# --------------------------------------------------------------------------


def test_score_risk_returns_bounded_score() -> None:
    out = score_risk(applicant_id="APP-00001", peril="hurricane")
    assert 0.0 <= out["score"] <= 100.0
    assert isinstance(out["factors"], list)


def test_score_risk_ranks_high_risk_above_clean_applicant() -> None:
    clean = score_risk(applicant_id="APP-00004", peril="water")
    risky = score_risk(applicant_id="APP-00003", peril="fire")
    assert risky["score"] > clean["score"]


def test_pull_loss_history_counts_events() -> None:
    assert pull_loss_history(applicant_id="APP-00005")["count"] == 2
    assert pull_loss_history(applicant_id="APP-00004")["count"] == 0


def test_unknown_applicant_returns_error_not_exception() -> None:
    assert "error" in score_risk(applicant_id="APP-99999", peril="fire")


# --------------------------------------------------------------------------
# pricing-mcp
# --------------------------------------------------------------------------


def test_lookup_base_rate_known_combination() -> None:
    out = lookup_base_rate(state="FL", product="homeowners", peril="hurricane")
    assert out["rate_per_1000"] > 0


def test_lookup_base_rate_unknown_combination_is_graceful() -> None:
    out = lookup_base_rate(state="FL", product="homeowners", peril="earthquake")
    assert "error" in out
    assert out["rate_per_1000"] == 0.0


def test_premium_is_monotonic_in_risk_score() -> None:
    low = calculate_premium(coverage_amount_usd=500_000, base_rate_per_1000=4.0, risk_score=20.0)
    high = calculate_premium(coverage_amount_usd=500_000, base_rate_per_1000=4.0, risk_score=90.0)
    assert high["final_premium_usd"] > low["final_premium_usd"]


def test_credit_band_a_produces_credit_not_surcharge() -> None:
    out = apply_modifiers(
        base_premium_usd=1000.0, risk_loading_usd=0.0, credit_band="A",
        prior_claims=0, construction_year=2020, coastal_within_5km=False,
    )
    assert out["credits_usd"] > 0
    assert out["final_premium_usd"] < 1000.0


def test_neutral_modifiers_never_yield_negative_zero() -> None:
    """Regression: -min() produced -0.0, which rendered as '$-0.00'."""
    out = apply_modifiers(
        base_premium_usd=1000.0, risk_loading_usd=0.0, credit_band="B",
        prior_claims=0, construction_year=2020, coastal_within_5km=False,
    )
    assert out["credits_usd"] == 0.0
    assert not str(out["credits_usd"]).startswith("-")


# --------------------------------------------------------------------------
# underwriting-mcp local tools
# --------------------------------------------------------------------------


def test_lookup_applicant() -> None:
    assert lookup_applicant(applicant_id="APP-00001")["state"] == "FL"


def test_eligibility_refers_on_claims_history() -> None:
    out = check_eligibility(applicant_id="APP-00003", product="homeowners", state="CA")
    assert out["outcome"] == "refer_to_underwriter"
    assert any("prior claims" in r for r in out["reasons"])


def test_eligibility_clean_applicant_passes() -> None:
    out = check_eligibility(applicant_id="APP-00004", product="homeowners", state="NY")
    assert out["outcome"] == "eligible"
    assert out["reasons"] == []


def test_eligibility_declines_coverage_above_capacity() -> None:
    """Above the filed capacity is a decline, not a referral."""
    out = check_eligibility(
        applicant_id="APP-00002", product="homeowners", state="TX",
        coverage_amount_usd=5_000_000.0,
    )
    assert out["outcome"] == "declined"
    assert any("capacity" in r for r in out["reasons"])


def test_eligibility_capacity_check_skipped_when_coverage_omitted() -> None:
    """The capacity rule is opt-in, so the tool stays usable without a limit."""
    out = check_eligibility(applicant_id="APP-00002", product="homeowners", state="TX")
    assert out["outcome"] == "eligible"


def test_capacity_decline_outranks_referral() -> None:
    """An applicant who would otherwise be referred is still declined.

    Ordering matters: a referral means 'a human should look at this', a
    decline means 'there is nothing to look at'. Capacity wins.
    """
    out = check_eligibility(
        applicant_id="APP-00003", product="homeowners", state="CA",
        coverage_amount_usd=9_000_000.0,
    )
    assert out["outcome"] == "declined"


# --------------------------------------------------------------------------
# orchestrator
# --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_orchestrator_produces_full_quote() -> None:
    gw = DirectGateway()
    quote = await run_quote(
        "APP-00002", "homeowners", "TX", 400_000.0, "wind", gateway=gw
    )
    assert quote["decision"] == "quote"
    assert quote["premium"]["final_premium_usd"] > 0
    assert quote["risk_score"]["score"] > 0


@pytest.mark.anyio
async def test_orchestrator_delegates_in_expected_order() -> None:
    """The supervisor must consult risk before pricing - premium depends on score."""
    gw = DirectGateway()
    await run_quote("APP-00002", "homeowners", "TX", 400_000.0, "wind", gateway=gw)

    called = [f"{srv}.{tool}" for srv, tool, _ in gw.calls]
    assert called == [
        "risk.score_risk",
        "risk.pull_loss_history",
        "pricing.lookup_base_rate",
        "pricing.calculate_premium",
        "pricing.apply_modifiers",
    ]


@pytest.mark.anyio
async def test_orchestrator_refers_high_risk_applicant() -> None:
    gw = DirectGateway()
    quote = await run_quote(
        "APP-00003", "homeowners", "CA", 500_000.0, "fire", gateway=gw
    )
    assert quote["decision"] == "refer"
    assert quote["risk_score"]["score"] > 50.0


@pytest.mark.anyio
async def test_rationale_records_delegations() -> None:
    gw = DirectGateway()
    quote = await run_quote(
        "APP-00001", "homeowners", "FL", 750_000.0, "hurricane", gateway=gw
    )
    assert "Delegated to:" in quote["rationale"]
    assert "risk.score_risk" in quote["rationale"]


@pytest.mark.anyio
async def test_declined_risk_costs_zero_peer_calls() -> None:
    """A decline short-circuits before risk and pricing.

    Regression: the declined branch was unreachable, so this edge of the
    graph was never exercised.
    """
    gw = DirectGateway()
    quote = await run_quote(
        "APP-00002", "homeowners", "TX", 5_000_000.0, "wind", gateway=gw
    )
    assert quote["decision"] == "decline"
    assert gw.calls == [], "declined risks must not spend peer round-trips"
    assert quote["risk_score"] is None
    assert quote["premium"] is None


@pytest.mark.anyio
async def test_aggregate_paid_losses_force_referral() -> None:
    """Severity, not just frequency.

    APP-00005 has only two claims, so the 3+ count rule passes it. Their
    combined $90,500 of paid losses should still refer - and that total is
    only visible through the `pull_loss_history` delegation.
    """
    gw = DirectGateway()
    quote = await run_quote(
        "APP-00005", "homeowners", "FL", 600_000.0, "hurricane", gateway=gw
    )
    assert quote["eligibility"]["outcome"] == "eligible"
    assert quote["decision"] == "refer"
    assert "90,500" in quote["rationale"]


@pytest.mark.anyio
async def test_loss_history_reaches_the_rationale() -> None:
    """The second risk delegation must affect output, not just call count."""
    gw = DirectGateway()
    quote = await run_quote(
        "APP-00001", "homeowners", "FL", 750_000.0, "hurricane", gateway=gw
    )
    assert "Loss history: 1 event(s)" in quote["rationale"]


class _FailingGateway:
    """Gateway double whose peer dies on a named tool."""

    def __init__(self, fail_on: str) -> None:
        self.fail_on = fail_on
        self.delegate = DirectGateway()

    async def call(self, server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool == self.fail_on:
            raise PeerUnavailable(f"{server}.{tool} failed: ConnectionResetError")
        return await self.delegate.call(server, tool, args)


@pytest.mark.anyio
@pytest.mark.parametrize("fail_on", ["score_risk", "lookup_base_rate", "apply_modifiers"])
async def test_peer_failure_refers_instead_of_auto_quoting(fail_on: str) -> None:
    """Fail closed. An unreachable peer must never yield an automatic quote.

    APP-00002 is the cleanest applicant in the set and quotes normally, so
    any decision other than `quote` here is attributable to the outage.
    """
    quote = await run_quote(
        "APP-00002", "homeowners", "TX", 400_000.0, "wind",
        gateway=_FailingGateway(fail_on=fail_on),
    )
    assert quote["decision"] == "refer"
    assert "Peer agent unavailable" in quote["rationale"]


@pytest.mark.anyio
async def test_pricing_skipped_once_risk_peer_is_down() -> None:
    """Don't compound an outage with a second doomed round-trip."""
    gw = _FailingGateway(fail_on="score_risk")
    await run_quote("APP-00002", "homeowners", "TX", 400_000.0, "wind", gateway=gw)

    called = [tool for _, tool, _ in gw.delegate.calls]
    assert called == [], "no pricing calls should follow a dead risk peer"


@pytest.mark.anyio
async def test_missing_filed_rate_forces_referral_not_zero_premium() -> None:
    """A peril with no rate table must refer, never publish a $0 quote.

    Regression: pricing returned 0.0 for an unratable peril and the
    orchestrator passed it straight through as a valid quote.
    """
    gw = DirectGateway()
    # APP-00004 is otherwise clean and would normally auto-quote.
    quote = await run_quote(
        "APP-00004", "homeowners", "NY", 350_000.0, "earthquake", gateway=gw
    )
    assert quote["decision"] == "refer"
    assert "Pricing gap" in quote["rationale"]
