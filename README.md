# Controlled Agent

A small local MVP/prototype for controlled AI tool use. The application
routes every tool action through an explicit deterministic control layer before
execution.

**The agent must never execute tools directly.**

User → agent proposal → governance → internal, exact-action-bound, single-use
authorization → executor → tool, with audit events throughout and human approval
when required.

## Status

Milestones 2A, 2B, 3A, 3B, 3C, 4A, and 4B are approved and committed. Milestone 4C
end-to-end verification passed on baseline `8697607b3fe25ae3a84d6903ace0326ed79897b0`:
228 tests passed on Python 3.12.14, with automated pseudo-terminal smoke tests and
observed fake-state/JSONL checks. The local prototype is ready for a scripted
demonstration, not production deployment. See the
[4C verification summary](ARCHITECTURE.md#milestone-4c-end-to-end-verification)
for evidence and limitations. Pure governance still returns
`GovernanceResult` data. The host-only service calls governance itself and issues
references after ALLOW or explicit human approval of REQUIRE_APPROVAL, with all
mandatory audit writes. The executor consumes each reference once and audits
execution start before calling its trusted handler. The new JSONL writer validates,
appends, flushes, and fsyncs every record. The CLI connects a bounded scripted
proposal source to the existing control boundary. Milestone 5A has approved and
documented the future LLM/provider integration architecture only: a future adapter
must submit the existing untrusted proposal envelope through `Controller.submit`.
Milestone 5B implementation has not started. No provider, API, dependency, runtime
behavior, or CLI behavior has been added, and no third-party dependencies are
required.

Constructing an `Authorization`, even with `USABLE` state and a UUID, grants no
execution rights. The service checks its private registry, never caller-created
records. Its references are identity-bound objects, distinct from auditable UUIDs.

The MVP uses Python 3.12+, one local process, a CLI, scripted proposals,
and local JSONL auditing. Scripted Demo remains the default. Future provider output
is untrusted: `no_action` and provider failures do not submit work, while a valid
plain proposal still enters the existing controller/governance boundary. Managed
provider experimentation remains unimplemented and requires separate authorization.

Run all tests from the repository root with Python 3.12+:

```sh
python3.12 -B -m unittest discover -s tests -v
```

The `controlled_agent/` package separates `contracts.py`, the domain-neutral
`governance.py`, fake work-order rules/configuration in `demo.py`, and the internal
`authorization.py` service. `approval.py` defines immutable review data and trusted
injected human I/O callbacks. `executor.py` implements the generic dispatch boundary;
`demo_tools.py` owns bounded mutable fake state. `audit.py` implements the separate
local JSONL writer. `submission.py` parses bounded proposal data; `controller.py`
orchestrates admission/execution; `cli.py` owns trusted terminal wiring. Tests live in `tests/`. `pyproject.toml` records project
metadata and the Python requirement; no installation or build setup is required.
Tests exercise execution counts, effects, and failure gates using both injected
test writers and real temporary JSONL files, including storage failure injection.
They do not establish power-loss survival or production security guarantees.

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
That writer must return `None` after successful mandatory persistence, or raise;
there is no default no-op. The 4A JsonlAuditWriter additionally requires fsync. `issue(request)` returns a reportable decision
and a separate host-only reference (or `None` when no authorization is issued). Never send
the tuple or reference to the proposal producer, or log the reference.

`consume(reference)` returns the exact stored action once, without revalidation
or argument replacement. Normal host execution uses `Executor.execute(reference)`,
which consumes within a shared service guard and successfully audits execution
start before dispatch. Errors never restore consumed authority.
Repeated action IDs, failed issuance, and reentrant operations cannot issue again;
the new `issue_proposal(proposal, action_id=..., caller=...)` API shares these
guarantees and audits before structural parsing. Host code supplies the ID/caller;
the proposal supplies neither. `issue(ActionRequest)` remains supported.

Approval is optional and configured only at construction: supply both
`approval_formatter=demo.format_work_order_approval` and
`human_approval=approval.HumanApprovalAdapter(display=..., read_response=...)`.
The trusted display callback receives an immutable `ApprovalReview` containing
the exact action identity and current/proposed state plus intended effect. It must
present the complete review and return `None`, or raise. The input callback must
collect one fresh human response. Only plain-string `approve`, with surrounding
whitespace allowed, is affirmative. `decline` refuses; empty/None/other responses,
EOF, errors, and interruptions never authorize. The CLI supplies console I/O;
the service itself still has no default input source,
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

## Run the local scripted demo

From the repository root on POSIX with Python 3.12+, provision a trusted local
runtime directory, then run:

```sh
mkdir -m 700 var
python3.12 -B -m controlled_agent
```

If `var` already exists, verify it is your trusted runtime directory rather than
changing its permissions automatically. The writer requires an existing parent and
an owned mode-0600 regular audit file. `--audit-path /trusted/path/audit.jsonl`
selects a host-owned path at startup; proposals cannot change it. Use an already
ignored runtime directory (`var/`, `audit/`, or `logs/`); never commit runtime logs.

The menu offers `1` read WO-1001, `2` close WO-1001, `3` attempt to close protected
WO-9001, and `q` quit. Other choices create no action or audit submission. The real
entry point requires interactive terminal input/output; no auto-approve option or
piped approval is supported. Host tests inject trusted streams into `run_session`.

Each action gets a fresh UUID and fixed caller `demo_operator`. Approval displays
canonical identity, fixed reviewed baseline, proposed status, and intended effect,
then flushes before reading one response. Type `approve` or `decline`. Empty/invalid
responses and EOF never authorize. Lines over 256 content characters terminate the
session; the CLI never approves a truncated prefix. Unterminated EOF input is also
cancelled. Approval data and raw input are never written to JSONL.

One service, fixture, executor, and writer serve the entire menu session. After an
approved status-changing update, reads show `closed` and later mutations may be
rejected with `STALE_BASELINE`. The session never refreshes the review snapshot or
resets action-ID tombstones. Governance denial and approval-not-granted return to
the menu; normally completed and audited no-mutation rejections also allow another
explicit selection. Reentrancy or incomplete completion processing terminates the
session while preserving the known no-mutation effect classification.
Unexpected execution, review, terminal, and audit errors stop the run. Effects are
reported separately from failed auditing; nothing is retried or claimed rolled back.
Exit codes: 0 normal quit/EOF, 1 operational failure, 2 command-line usage or missing
interactive terminal, 130 KeyboardInterrupt. A SystemExit raised by session callbacks is safely reported
as an interrupted operational failure (1), without printing its raw message.
Approval EOF cancels and exits.

The controller accepts only a plain `tool_name`/`arguments` envelope. The parser
limits tool/key lengths to 128 characters, string values to 256, argument count to
eight, integers to signed 64-bit, floats to finite values, and encoded proposals
to 4096 bytes. Nested containers, custom types, and top-level identity/authority
fields are rejected. Tool-specific arguments and policy are still checked by
governance. Malformed proposals are audited as DENY/MALFORMED_PROPOSAL with null
risk/target and no validated action. Submission tool names are null on this path
because auditing precedes parsing; later decision records retain resolved context.

Controller reports contain no references or raw exceptions. Tool results remain
untrusted data; the CLI prints only known demo IDs/status values, never arbitrary
result mappings. The current issuance API groups decline/cancellation as approval
not granted; the audit retains its more specific outcome. Terminal access does not
authenticate a human or prove that they viewed the review.

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

## Local JSONL audit persistence

Trusted host code can pass `audit.JsonlAuditWriter(path)` directly as `audit_write`
when constructing the service. Keep the service and executor inside the writer's
`with` scope; exiting closes the file. The host supplies the path and must provision
its existing trusted parent directory. `var/audit.jsonl` is a recommended runtime
location; `/var/`, `/audit/`, and `/logs/` are already ignored by Git.

This sink currently requires POSIX with `O_NOFOLLOW`. It opens in append mode,
creates files with mode `0600`, and checks the descriptor for a regular file owned
by the effective user with exactly that mode. A restrictive umask that removes
owner bits causes rejection. It rejects final-component symlinks, unsuitable
existing modes, and nonempty files without a terminating newline. It never changes
permissions or repairs a tail. Ancestors, ACLs, path access, and exclusive writer
ownership remain trusted deployment responsibilities.

Each line has action ID, UTC timestamp, event type, caller ID (or null), and exact
allowlisted details. Public identifiers and reason codes are bounded to 128 ASCII
characters; the entire encoded line is limited to 4 KiB. Extra fields and unsupported
values fail the write; arguments, results, reviews, responses, and private references
are never persisted. Identifier syntax cannot detect secrets hidden in permitted
strings. See the [field policy](ARCHITECTURE.md#milestone-4a-jsonl-writer).

The writer validates/encodes before appending, checks the full byte count, then
flushes and calls `os.fsync` for every event. Any validation, serialization, I/O, or
interruption failure permanently disables that writer. Short writes may leave a
partial tail; flush/sync failures may leave a complete visible record. Neither
permits automatic retry, authority reconstruction, or an execution-success claim.
Original interruptions propagate; context cleanup does not mask an earlier failure.

`fsync` improves persistence but is not an absolute power-loss guarantee; new parent
directory entries are not synced. JSONL is neither tamper-resistant nor transactional
with tool effects. A newline-terminated existing tail does not authenticate or
validate historical records. Concurrent writers, rotation/recovery, and log-based
approval or authorization recovery are outside scope.

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

Authorization and approval state expire on restart. Local JSONL
audit logs persist without conveying execution rights.

Industrial operations are the demonstration layer; the governance core remains
domain-neutral. No real industrial systems or cloud services are connected.
This learning prototype is not a production-ready system or a security sandbox;
arbitrary malicious code inside its process is outside the threat model.

## Project documents

- [Project brief](PROJECT_BRIEF.md): goals, scope, success criteria, and limitations.
- [Architecture](ARCHITECTURE.md): contracts, control flow, trust boundaries, and audit behavior.
- [Decisions](DECISIONS.md): choices and their reasoning.
- [Repository instructions](AGENTS.md): constraints for future coding agents.
