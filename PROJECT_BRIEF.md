# Controlled Agent

## Problem

An agent that can invoke tools directly can turn an unsafe proposal into a real
side effect. Instructions to an LLM are not an enforceable permission or policy
boundary. Applications need an explicit control layer between proposals and
execution, including visibility into rejected attempts.

## Goal

Build a small, production-minded local MVP that demonstrates controlled tool
execution, and document the architectural decisions behind it. The first phase
uses scripted proposals so the control boundary can be verified independently
of model behavior. Model integration follows only after that boundary works.

## Core principle

**The agent must never execute tools directly.**

User → proposal → governance → internal authorization → executor → tool.

Human approval is obtained when governance requires it. Audit events accompany
the entire lifecycle, including malformed proposals and denied actions.

## MVP scope

- Python 3.12+, one local application, a CLI, and explicit module boundaries.
- Deterministic validation, permissions, policies, and risk classification.
- Decisions: `ALLOW`, `DENY`, and `REQUIRE_APPROVAL`.
- One-time human approval of the exact validated action, without overriding
  hard policy denials.
- Internal, single-use execution authorization bound to the validated tool,
  arguments, target, action ID, and trusted caller context.
- Two bounded tools using fake local work orders; no industrial connection.
- Local JSONL audit storage, with failure to write required pre-execution
  records preventing execution.
- In-memory approval and authorization state that expires on process exit.
- Focused tests of the boundary and documentation of meaningful decisions.

## Demonstration

Industrial operations provide the sample domain, not the vocabulary or logic of
the governance core. Work-order schemas, status rules, and protection metadata
belong to the demo's tool definitions and policy functions.

| Proposal | Condition | Risk | Decision | Expected effect |
| --- | --- | --- | --- | --- |
| `get_work_order` | Allowed sample target; valid arguments; caller has permission | LOW | `ALLOW` | Return the fake work order |
| `update_work_order_status` | Normal editable sample target; valid status; caller has permission | MEDIUM | `REQUIRE_APPROVAL` | Update only after explicit approval |
| Any attempted mutation | Protected or critical sample target, including a proposed close | HIGH | `DENY` | No approval prompt and no tool invocation |

Use the same update tool for editable and protected targets. Closing is a status
change; a separate close tool adds no useful boundary to this demonstration.
Governance considers the target, arguments, permissions, and policy, so the same
tool can receive different decisions. Risk classification never grants permission.
The three demo paths are not a universal mapping from risk to decision.

The configured caller is `demo_operator`; `WO-1001` is editable and `WO-9001` is
protected and critical. [ARCHITECTURE.md](ARCHITECTURE.md) records these confirmed
choices and the remaining sample conventions for review. The fake local data
does not model a real industrial workflow.

## Success criteria

1. A permitted LOW-risk read executes and produces a correlated audit trail.
2. A permitted editable status update is MEDIUM risk and requires approval of
   the exact work order and proposed status; approval permits one execution,
   while decline or cancellation permits none.
3. Every identifiable mutation of a protected or critical work order is HIGH
   risk and denied, even when a caller claims approval or requests `closed`.
4. Missing permissions, unknown tools or targets, and malformed proposals fail
   before a handler is invoked; each submission receives an action ID.
5. Missing, forged, altered, pending, denied, or consumed authorizations cannot
   cause tool execution. A caller-supplied `approved=true` has no authority.
6. Submitted, malformed, denied, approved, declined, execution-started,
   succeeded, and failed paths are observable. Pre-execution audit failures
   prevent execution; post-execution uncertainty is reported honestly.
7. The governance core contains no work-order-specific conditions, and replacing
   the scripted producer with a future model adapter does not bypass governance.

## Explicit non-goals

No cloud deployment, real industrial integration, databases, queues,
microservices, user authentication system, multi-tenant platform, policy
language, vector database, persistent agent memory, or multi-agent system.

No unrestricted shell or filesystem tools, autonomous tool dispatch by an SDK,
LLM-based governance decisions, parallel execution, automatic retries, durable
approval recovery, or exactly-once execution guarantees. Model/provider
integration and third-party dependencies remain outside the current milestone.

## Security boundary and limitations

The planned MVP is designed to reject unsafe proposals and catch application
mistakes through its defined interfaces. It cannot contain arbitrary malicious
code or an implementation change that introduces a direct tool-call path inside
the same process. Module boundaries and internal authorization are application
controls, not OS isolation or a security sandbox. This is a learning prototype,
not a production-ready system.

Tools and policy code are trusted application code. Bounded handlers must enforce
their declared resource limits; schemas alone cannot contain arbitrary handler
behavior. Sample protection metadata and policy configuration stay fixed during
a run. There is no claim of safety for concurrent external resource changes.

A local JSONL file is neither tamper-proof nor transactional with tool effects.
Storage failures can prevent durable audit records, and a crash after dispatch
can leave the outcome uncertain. The application must fail closed before
execution when logging fails and must not automatically retry uncertain effects.

## Current status

Milestone 2A contracts are approved and committed. Milestone 2B adds deterministic
governance for host-assembled requests, permission checks, target resolution,
argument validation, contextual policy/risk, and a correlated governance result.
The fake work-order metadata and pure rules are separate from the domain-neutral
core. This implementation and its tests are ready for review, using only the
Python standard library.

Evaluation returns a decision without executing anything or granting authority.
Raw proposal parsing, CLI, approval workflow, authorization issuance/consumption,
executor, tool handlers, audit storage, and model integration remain deferred.
The full execution and audit boundary is not yet implemented.
