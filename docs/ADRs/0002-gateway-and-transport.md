# ADR 0002 — Gateway abstraction for agent-to-agent transport

**Status:** Accepted
**Date:** 2026-06-14
**Supersedes:** the in-process import shortcut used in v0.1

## Context

ADR-0001 established three MCP servers with an orchestrator inside `underwriting-mcp`. It did not say *how* the orchestrator reaches its peers.

The v0.1 scaffold took a shortcut — the orchestrator imported peer tool functions directly:

```python
from mcp_uw_workbench.risk.server import score_risk   # v0.1
```

That is not agent-to-agent communication. It is a function call in one process wearing a costume. Anyone reading `orchestrator.py` would see it immediately, and the repository's entire stated purpose — demonstrating A2A over MCP — would be undercut by its own source.

At the same time, routing every unit-test assertion through subprocess spawn is slow enough to discourage running tests, which has its own failure mode.

## Decision

Introduce a `ToolGateway` protocol in `gateway.py` with two implementations:

| Implementation | Transport | Used by |
|---|---|---|
| `MCPGateway` | Real MCP over stdio; each peer is a subprocess | Production, `generate_quote`, integration tests |
| `DirectGateway` | In-process function calls; records call order | Unit tests and the eval harness |

The orchestrator depends only on the protocol. `run_quote()` opens an `MCPGateway` when no gateway is supplied, so **the default path is real MCP** — the shortcut is opt-in, not opt-out.

Peer processes are launched as `sys.executable -m mcp_uw_workbench.<peer>.server` rather than by console-script name, so the gateway works from a venv, a container, or a test runner without depending on `PATH`.

### The error contract

The gateway also owns the boundary between *a peer that answered badly* and
*a peer that did not answer*:

- A peer returning `{"error": ...}` is **data**. It crosses back as a normal
  result and the orchestrator decides what it means — an unknown applicant
  and a missing filed rate are business outcomes, not outages.
- A peer that cannot start, dies mid-call, breaks its stdio pipe, or exceeds
  `timeout_s` raises **`PeerUnavailable`**. Callers cannot mistake one for
  the other, which is what lets the orchestrator fail closed on outages
  while still passing business errors through.

Cancellation is explicitly excluded from that conversion — it belongs to the
caller and must propagate untouched.

One subtlety worth recording, because it is easy to reintroduce: the
connection timeout wraps only the MCP `initialize()` handshake, **not** the
`enter_async_context` calls that register peers on the `AsyncExitStack`.
Those contexts are exited later, outside the cancel scope, and anyio raises
`Attempted to exit a cancel scope that isn't the current task's` if a scope
spans their entry but not their exit.

## Consequences

### Positive

- The A2A claim is now literally true. Every delegation in `orchestrator.py` crosses a process boundary and speaks MCP.
- Transport is swappable in one file. Moving stdio → SSE → streamable HTTP touches `gateway.py` and nothing else.
- Unit tests stay fast (~1.5s) while integration tests still exercise the real protocol.
- `DirectGateway` records `(server, tool, args)` for every call, which made delegation-order assertions and the evaluation harness possible. That was an unplanned benefit.

### Negative

- Two code paths mean the test double can drift from real behaviour. Mitigated by `test_direct_and_mcp_gateways_agree`, which runs the same scenario through both and asserts identical output. If they ever diverge, that test fails.
- `MCPGateway` holds an `AsyncExitStack` and is therefore context-manager-bound. Calling `.call()` outside the context raises rather than lazily connecting — deliberate, because silent reconnection hides lifecycle bugs.
- Subprocess-per-peer does not survive to production as-is. A real deployment would run peers as long-lived services over SSE or streamable HTTP; ADR-0003 will cover that when v0.4 lands.

## Alternatives considered

1. **Real MCP everywhere, including unit tests.** Purest, and rejected on ergonomics — a ~14s test suite for logic assertions discourages running it.
2. **Mock the MCP client library.** Would test against an idealised protocol rather than the real one, and mocks drift silently. `DirectGateway` at least executes the same tool functions the servers execute.
3. **HTTP/SSE from the start.** More production-shaped but adds port allocation, health checks, and startup ordering to a repo whose point is the orchestration pattern, not the deployment topology.

## Verification

`tests/test_mcp_integration.py` proves the decision holds:

- `test_peers_advertise_expected_tools` — capability discovery over MCP
- `test_tool_call_round_trip_over_stdio` — a call crosses a process boundary
- `test_peer_errors_propagate_as_data` — a bad input returns a payload, not a fault
- `test_unstartable_peer_raises_peer_unavailable` — a peer that cannot start is a fault, not a payload
- `test_end_to_end_quote_over_real_mcp` — full supervisor → peer → peer flow
- `test_direct_and_mcp_gateways_agree` — the two implementations do not diverge
