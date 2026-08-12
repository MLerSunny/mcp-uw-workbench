"""Evaluation harness for the orchestrator.

Scores the supervisor on the dimensions described in EVALUATION.md:

  * delegation accuracy - did it call the right peer tools, in order?
  * decision quality    - did the final decision match ground truth?
  * efficiency          - did it make unnecessary calls?

Run with:  pytest tests/eval -q -s

Each scenario runs through `DirectGateway` so the harness stays fast and
deterministic; `test_mcp_integration.py` separately proves the two
gateways agree, so accuracy measured here transfers to the real transport.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mcp_uw_workbench.gateway import DirectGateway
from mcp_uw_workbench.underwriting.orchestrator import run_quote

SCENARIOS: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "scenarios.json").read_text()
)

# SLOs from EVALUATION.md
MIN_DELEGATION_ACCURACY = 0.95
MIN_DECISION_ACCURACY = 0.90

RISK_BANDS = {"low": (0.0, 45.0), "medium": (45.0, 75.0), "high": (75.0, 100.0)}


async def _run(scenario: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    gw = DirectGateway()
    quote = await run_quote(**scenario["input"], gateway=gw)
    return quote, [f"{srv}.{tool}" for srv, tool, _ in gw.calls]


@pytest.mark.anyio
@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s["id"])
async def test_scenario_delegation_and_decision(scenario: dict[str, Any]) -> None:
    quote, delegations = await _run(scenario)

    assert delegations == scenario["expected_delegations"], (
        f"{scenario['id']}: wrong delegation sequence"
    )
    assert quote["decision"] == scenario["expected_decision"], (
        f"{scenario['id']}: expected {scenario['expected_decision']}, "
        f"got {quote['decision']}"
    )

    band = scenario["expected_risk_band"]
    if band != "any" and quote.get("risk_score"):
        lo, hi = RISK_BANDS[band]
        score = quote["risk_score"]["score"]
        assert lo <= score <= hi, f"{scenario['id']}: risk {score} outside {band} band"


@pytest.mark.anyio
async def test_aggregate_slos_are_met(capsys: pytest.CaptureFixture[str]) -> None:
    """Fleet-level scorecard. Fails the build if the suite regresses."""
    delegation_hits = 0
    decision_hits = 0
    rows: list[str] = []

    for scenario in SCENARIOS:
        quote, delegations = await _run(scenario)
        deleg_ok = delegations == scenario["expected_delegations"]
        dec_ok = quote["decision"] == scenario["expected_decision"]
        delegation_hits += deleg_ok
        decision_hits += dec_ok
        rows.append(
            f"  {scenario['id']:<32} "
            f"delegation={'PASS' if deleg_ok else 'FAIL'}  "
            f"decision={'PASS' if dec_ok else 'FAIL'} "
            f"({quote['decision']})"
        )

    n = len(SCENARIOS)
    delegation_acc = delegation_hits / n
    decision_acc = decision_hits / n

    with capsys.disabled():
        print("\n\n=== Orchestrator evaluation ===")
        print("\n".join(rows))
        print(f"\n  delegation accuracy : {delegation_acc:.0%} (SLO {MIN_DELEGATION_ACCURACY:.0%})")
        print(f"  decision accuracy   : {decision_acc:.0%} (SLO {MIN_DECISION_ACCURACY:.0%})")
        print(f"  scenarios           : {n}\n")

    assert delegation_acc >= MIN_DELEGATION_ACCURACY
    assert decision_acc >= MIN_DECISION_ACCURACY


@pytest.mark.anyio
async def test_no_redundant_peer_calls() -> None:
    """Efficiency: the supervisor should never call the same tool twice."""
    for scenario in SCENARIOS:
        _, delegations = await _run(scenario)
        assert len(delegations) == len(set(delegations)), (
            f"{scenario['id']}: duplicate delegations {delegations}"
        )
