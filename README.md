# Controlled Agent

A small local MVP/prototype for controlled AI tool use. The planned application
routes every tool action through an explicit deterministic control layer before
execution.

**The agent must never execute tools directly.**

User → agent proposal → governance → internal, exact-action-bound, single-use
authorization → executor → tool, with audit events throughout and human approval
when required.

## Status

Milestones 2A, 2B, 3A, and 3B are approved and committed. Milestone 3C executor
and bounded fake tools are implemented for review. Pure governance still returns
`GovernanceResult` data. The host-only service calls governance itself and issues
references after ALLOW or explicit human approval of REQUIRE_APPROVAL, with all
mandatory audit writes. The executor consumes each reference once and audits
execution start before calling its trusted handler. Concrete audit storage, CLI,
and LLM integration remain deferred. No third-party dependencies are required.

Constructing an `Authorization`, even with `USABLE` state and a UUID, grants no
execution rights. The service checks its private registry, never caller-created
records. Its references are identity-bound objects, distinct from auditable UUIDs.

The planned MVP uses Python 3.12+, one local process, a CLI, scripted proposals,
and local JSONL auditing. Model/provider integration is deferred until the
deterministic execution boundary works.

Run all tests from the repository root with Python 3.12+:

```sh
python3.12 -B -m unittest discover -s tests -v
```

The `controlled_agent/` package separates `contracts.py`, the domain-neutral
`governance.py`, fake work-order rules/configuration in `demo.py`, and the internal
`authorization.py` service. `approval.py` defines immutable review data and trusted
injected human I/O callbacks. `executor.py` implements the generic dispatch boundary;
`demo_tools.py` owns bounded mutable fake state. Tests live in `tests/`. `pyproject.toml` records project
metadata and the Python requirement; no installation or build setup is required.
Tests exercise execution counts, effects, and failure gates using injected test
writers. They do not establish durable storage or production security guarantees.

The evaluation entry points are `governance.evaluate_action` and
`demo.evaluate_demo`. They accept an `ActionRequest` whose ID and caller were
supplied by trusted application code. Raw proposal dictionaries are rejected;
argument claims cannot supply identity, permission, approval, or policy results.
Targets are resolved from tool arguments against trusted resource metadata;
neither a conflicting `ActionRequest.target` nor claimed protection flags can
create or replace that context. Unknown targets fail closed without an invented
risk classification.

Trusted host code constructs `authorization.AuthorizationService` once per run
with fixed configuration and a required synchronous `audit_write(event)` function.
That writer must return `None` after successful write/flush, or raise; 3A provides
no concrete sink or default no-op. `issue(request)` returns a reportable decision
and a separate host-only reference (or `None` when no authorization is issued). Never send
the tuple or reference to the proposal producer, or log the reference.

`consume(reference)` returns the exact stored action once, without revalidation
or argument replacement. Normal host execution uses `Executor.execute(reference)`,
which consumes within a shared service guard and successfully audits execution
start before dispatch. Errors never restore consumed authority.
Repeated action IDs, failed issuance, and reentrant operations cannot issue again;
raw proposal parsing remains outside this API.

Approval is optional and configured only at construction: supply both
`approval_formatter=demo.format_work_order_approval` and
`human_approval=approval.HumanApprovalAdapter(display=..., read_response=...)`.
The trusted display callback receives an immutable `ApprovalReview` containing
the exact action identity and current/proposed state plus intended effect. It must
present the complete review and return `None`, or raise. The input callback must
collect one fresh human response. Only plain-string `approve`, with surrounding
whitespace allowed, is affirmative. `decline` refuses; empty/None/other responses,
EOF, errors, and interruptions never authorize. There is no default console I/O,
public approval endpoint, or pending token. Missing approval configuration keeps
the 3A behavior: REQUIRE_APPROVAL returns no reference.

The returned governance decision stays REQUIRE_APPROVAL even after successful
approval; the separate opaque reference represents issuance. Pending state closes
before the approval-result audit. Audit failure or interruption cannot restore it
or permit reissuance. DENY never prompts. The display uses the fixed trusted
resource snapshot, not live resource state; external freshness/TOCTOU protection
is not implemented. Callbacks remain trusted to present faithfully and obtain
human input, and do not authenticate a person or prove that a screen was viewed.

Audit events retain bounded public tool/target identifiers, with submission claims
separate from resolved context and full-action validation status. DENY and
REQUIRE_APPROVAL retain known resolution context too. Unknown claims and argument
payloads are omitted, including from issuance events; exact arguments remain in
the private action record. Approval audits add fixed outcomes/reason codes, never
review text or raw responses. See the [3A retention policy](ARCHITECTURE.md#milestone-3a-audit-retention-policy).

## Executor and fake tools

Trusted host wiring uses the same service for the fixture and executor:

```python
from controlled_agent.demo_tools import FakeWorkOrderTools
from controlled_agent.executor import Executor

# service is the host's existing configured AuthorizationService.
fixture = FakeWorkOrderTools(authorization=service)
executor = Executor(authorization=service, handlers=fixture.handlers)
# Only an internally issued reference may be passed to executor.execute(reference).
```

Use one service, fixture, and executor per run. Never give the proposal producer
these objects or their handlers. The executor accepts no action or replacement
arguments. The handlers receive the exact retained action and independently check
resource bounds, target consistency, supported statuses, and protection. They
perform no filesystem, network, console, or industrial I/O.

Reads return immutable snapshots of live fake state. Updates change only status.
Immediately before mutation, the handler compares live status against the fixed
baseline captured by its authorization service. A mismatch produces STALE_BASELINE
without changing state; the reference stays consumed. After a status-changing
update, further mutations are therefore rejected for that run, even after fresh
approval. Same-status updates remain possible while the baseline matches. Review
still shows the fixed snapshot, not live state. No refresh/reset bypass or external
TOCTOU protection is provided.

`ExecutionReport` separates completed handler execution from completion auditing.
`ExecutionError.report` distinguishes no dispatch, known rejection without mutation,
and potentially partial failure; it can also report a completed effect with an audit
failure. Audit and handler errors remain separate host-only diagnostic fields.
KeyboardInterrupt/SystemExit propagate with `execution_report` and diagnostic
attributes. Do not expose diagnostic tracebacks as agent-facing output. Results are
immutable data and are never automatically logged or interpreted as authority.

An execution-start audit failure prevents all handler calls. Post-dispatch errors
never promise rollback or trigger retry. Reentrant calls poison the shared operation;
pre-dispatch detection blocks invocation, while post-dispatch detection preserves the
known/uncertain outcome and skips further callbacks. Audit trails can be incomplete.
Execution events retain bounded identifiers and fixed outcome/reason codes only.

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

Authorization and approval state expire on restart. Future local
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
