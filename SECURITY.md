# Security & Data Handling

## Data policy

**This repository contains synthetic data only.** Names, addresses, applicant identifiers, claim records, rate tables, and peril taxonomies are all fabricated for demonstration purposes.

- No real PII (Personally Identifiable Information).
- No real PHI (Protected Health Information).
- No real policy, claims, or actuarial data.
- No data sourced from any past employer.

## When deploying for real

If you adapt this workbench against real applicant or policy data, the following hardening is required before any production use:

1. **PII scrubbing at MCP-tool boundary.** Apply structured PII detection before any data crosses the MCP transport (Claude or other LLM client receives only redacted or tokenized values).
2. **Audit logging.** Every MCP tool call should be logged with caller identity, timestamp, parameters (with PII redacted), and decision rationale.
3. **Bedrock Guardrails / equivalent policy filter** on prompts returning underwriting decisions.
4. **Human-in-the-loop gating** for all `decline` and `refer_to_underwriter` outcomes before customer-facing communication.
5. **State-DOI compliance.** Underwriting decisions must comply with the originating state's insurance regulations; the synthetic rule set in this repo is illustrative only.
6. **SR 11-7 model risk management** (if used by a US financial institution): model card, validation, and ongoing monitoring for the risk-scoring model.
7. **Rate-table integrity.** Production rate tables must be version-controlled and reconciled against state filings.

## Reporting a security issue

For now: open a GitHub issue prefixed `[SECURITY]`. For a production fork, set up a dedicated security mailbox.
