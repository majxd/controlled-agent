# Controlled Agent

A small local MVP/prototype for controlled AI tool use. The planned application
routes every tool action through an explicit deterministic control layer before
execution.

**The agent must never execute tools directly.**

User → agent proposal → governance → internal, exact-action-bound, single-use
authorization → executor → tool, with audit events throughout and human approval
when required.

## Status

Milestone 2A contracts are approved and committed. Milestone 2B deterministic
governance and its tests are implemented for review. Governance evaluates tool
existence, arguments, target context, permissions, policy, and contextual risk.
It returns `GovernanceResult` data without executing tools or issuing authority.
Approval, execution, tool handlers, audit storage, CLI, and LLM integration remain
deferred. No third-party dependencies are required.

Constructing an `Authorization`, even with `USABLE` state and a UUID, grants no
execution rights. Future execution must resolve an internal reference against
governance-owned issuance state; it must never trust a caller-created record.

The planned MVP uses Python 3.12+, one local process, a CLI, scripted proposals,
and local JSONL auditing. Model/provider integration is deferred until the
deterministic execution boundary works.

Run all tests from the repository root with Python 3.12+:

```sh
python3.12 -B -m unittest discover -s tests -v
```

The `controlled_agent/` package separates `contracts.py`, the domain-neutral
`governance.py`, and fake work-order rules/configuration in `demo.py`. Tests live
in `tests/`. `pyproject.toml` records project metadata and the Python requirement;
no installation or build setup is required. Passing these tests does not establish
the deferred execution and audit boundary.

The evaluation entry points are `governance.evaluate_action` and
`demo.evaluate_demo`. They accept an `ActionRequest` whose ID and caller were
supplied by trusted application code. Raw proposal dictionaries are rejected;
argument claims cannot supply identity, permission, approval, or policy results.
Targets are resolved from tool arguments against trusted resource metadata;
neither a conflicting `ActionRequest.target` nor claimed protection flags can
create or replace that context. Unknown targets fail closed without an invented
risk classification.

## MVP demo

Current evaluation results for `demo_operator` with permission and valid arguments:

| Path | Risk | Decision / behavior |
| --- | --- | --- |
| `get_work_order` on `WO-1001` or `WO-9001` | LOW | `ALLOW` |
| `update_work_order_status` on editable `WO-1001` | MEDIUM | `REQUIRE_APPROVAL` |
| Any mutation of protected or critical `WO-9001`, including closure through the update tool | HIGH | `DENY`; no approval override or tool invocation |

The same tool can receive different decisions based on target, arguments,
permissions, and policy. Risk never grants permission; approval cannot override
`DENY`. Protected mutations stay HIGH / DENY even with invalid status, missing
permission, or claimed approval. Other unexpected argument fields are rejected.
Statuses are `open`, `in_progress`, and `closed`; even a same-status editable
update requires approval. No work-order state changes during evaluation.

Future approval and authorization state will expire on restart; future local
JSONL audit logs will persist without conveying execution rights.

Industrial operations are the demonstration layer; the governance core remains
domain-neutral. No real industrial systems or cloud services are connected.
This learning prototype is not a production-ready system or a security sandbox;
arbitrary malicious code inside its process is outside the threat model.

## Project documents

- [Project brief](PROJECT_BRIEF.md): goals, scope, success criteria, and limitations.
- [Architecture](ARCHITECTURE.md): contracts, control flow, trust boundaries, and audit behavior.
- [Decisions](DECISIONS.md): choices and their reasoning.
- [Repository instructions](AGENTS.md): constraints for future coding agents.
