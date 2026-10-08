# Controlled Agent

A small local MVP/prototype for controlled AI tool use. The planned application
routes every tool action through an explicit deterministic control layer before
execution.

**The agent must never execute tools directly.**

User → agent proposal → governance → internal, exact-action-bound, single-use
authorization → executor → tool, with audit events throughout and human approval
when required.

## Status

Milestone 2A: core data contracts and contract tests are implemented for review.
The contracts use immutable snapshots and structural validation. Governance,
approval, execution, tools, and audit storage are not implemented; there is no
runnable CLI or LLM integration yet. No third-party dependencies are required.

Constructing an `Authorization`, even with `USABLE` state and a UUID, grants no
execution rights. Future execution must resolve an internal reference against
governance-owned issuance state; it must never trust a caller-created record.

The planned MVP uses Python 3.12+, one local process, a CLI, scripted proposals,
and local JSONL auditing. Model/provider integration is deferred until the
deterministic execution boundary works.

Run the contract tests from the repository root with Python 3.12+:

```sh
python3.12 -B -m unittest discover -s tests -v
```

The initial package is `controlled_agent/`; contracts live in `contracts.py`
and tests in `tests/test_contracts.py`. `pyproject.toml` records project metadata
and the Python requirement; no installation or build setup is required for
these tests. Passing contract tests does not establish a working control gate.

## MVP demo

Two bounded tools will operate on fake local work orders for `demo_operator`:

| Path | Risk | Decision / behavior |
| --- | --- | --- |
| `get_work_order` on an allowed order, with valid arguments and permission | LOW | `ALLOW`; execute the read |
| `update_work_order_status` on editable `WO-1001`, with valid arguments and permission | MEDIUM | `REQUIRE_APPROVAL`; show the exact order and new status |
| Any mutation of protected or critical `WO-9001`, including closure through the update tool | HIGH | `DENY`; no approval override or tool invocation |

The same tool can receive different decisions based on target, arguments,
permissions, and policy. Risk never grants permission; approval cannot override
`DENY`. Approval and authorization expire on restart; local JSONL audit logs persist.

Industrial operations are the demonstration layer; the governance core remains
domain-neutral. No real industrial systems or cloud services are connected.
This learning prototype is not a production-ready system or a security sandbox;
arbitrary malicious code inside its process is outside the threat model.

## Project documents

- [Project brief](PROJECT_BRIEF.md): goals, scope, success criteria, and limitations.
- [Architecture](ARCHITECTURE.md): contracts, control flow, trust boundaries, and audit behavior.
- [Decisions](DECISIONS.md): choices and their reasoning.
- [Repository instructions](AGENTS.md): constraints for future coding agents.
