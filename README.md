# Controlled Agent

A small local MVP/prototype for controlled AI tool use. The application
routes every tool action through an explicit deterministic control layer before
execution.

**The agent must never execute tools directly.**

User → agent proposal → governance → internal, exact-action-bound, single-use
authorization → executor → tool, with audit events throughout and human approval
when required.

## Status

**Local MVP acceptance:** The current local MVP/prototype scope, including bounded
5B and 5C, was accepted and closed on 2026-10-31. The [acceptance matrix](ARCHITECTURE.md#local-mvp-acceptance--2026-10-31)
maps the seven brief criteria to existing implementation and tests. Earlier status
accounts below remain historical. This is not production readiness, broad model
quality, or live provider_demo verification; the earlier unexplained unavailable
failure remains unresolved. Further live inference needs separate authorization.

**5C closure:** Documentation-only closure of bounded synthetic evaluation was
approved on 2026-10-31. The [closure summary](ARCHITECTURE.md#milestone-5c-bounded-synthetic-evaluation--closure)
separates a user-attested eight-case live batch from offline verification and
retains source-unverified provenance. 5B stays closed. The batch does not verify
execution, broader model quality, or live
provider_demo behavior. Earlier status accounts below are historical.

**Current collection increment:** The bounded programmatic collector is implemented
with offline fixture verification, accepted after final review. It borrows an adapter,
visits the eight cases once, and retains scored results without executing tools.
Source provenance remains unverified; no live runner, CLI or persistence is added.
5B stays closed and overall 5C stays open. The scorer account below records its
earlier accepted scope; historical evidence and limitations remain unchanged.

**Current 5C increment:** The pure offline proposal scorer and eight-case synthetic
corpus are implemented with deterministic verification, accepted after final review.
Fixture and Mock results verify the evaluator, not Groq quality. 5B remains closed;
5C remains open. No provider runner, CLI, persistence or execution integration is
added. Historical status/evidence below remains unchanged.

Milestone 5B is closed by documentation-only review on 2026-10-26, limited to
the bounded one-shot integration in 5B.1–5B.4. Mock remains the provider-demo
default; the scripted CLI is unchanged. Milestone 5C remains open and unimplemented.
This adds no live verification of `provider_demo`, model-quality evaluation,
production-readiness or cost/privacy guarantee, or authorization for new inference.
See the [5B closure summary](ARCHITECTURE.md#milestone-5b-closure--2026-10-26).
The evidence and milestone status statements below retain their historical scope.

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
proposal source to the existing control boundary. Milestone 5A approved the
provider architecture; Milestone 5B.1 implements the offline provider interface,
immutable public request, structural adapter, and Mock Provider. The adapter
returns a copied plain `tool_name`/`arguments` proposal or None. Only the trusted
host submits a non-None result through its existing `Controller.submit`.
None alone means no_action; the string "no_action" is invalid. Provider failures
and malformed output stop before submission, with no action ID or execution-audit
events. Scripted Demo remains the CLI default. That offline 5B.1 increment added
no live API, credentials, dependency, or CLI expansion. See the
[5B.1 interface and host example](ARCHITECTURE.md#milestone-5b1-offline-provider-interface-and-mock-provider).

The reconciled 5B.1 suite passed 246 tests on Python 3.12.14 with `-B` on
2026-10-18, including 18 provider tests. This verifies the offline interface and
existing control boundary; live provider/model evaluation remains deferred.

Constructing an `Authorization`, even with `USABLE` state and a UUID, grants no
execution rights. The service checks its private registry, never caller-created
records. Its references are identity-bound objects, distinct from auditable UUIDs.

The MVP uses Python 3.12+, one local process, a CLI, scripted proposals,
and local JSONL auditing. Scripted Demo remains the default. Provider output
is untrusted: `no_action` and provider failures do not submit work, while a valid
plain proposal still enters the existing controller/governance boundary. Milestone
5B.2 implements an optional one-shot Groq provider. Its offline verification and
the separately user-reported connectivity/decoding-only live smoke test are
documented in ARCHITECTURE.md. Milestone 5B.3 adds programmatic host integration
below. The separately authorized one-shot 5B.4 demo is described below; further
live inference still requires separate authorization.

## Optional Groq provider (5B.2, offline-verified)

`controlled_agent.groq_provider.GroqProvider` implements the same proposal protocol
using experimental `openai/gpt-oss-20b`; this is not a production model commitment.
It returns a copied plain proposal or None to the existing adapter. Only trusted
host code submits non-None results through its existing controller. No core module,
scripted CLI behavior, hosted tool, automatic dispatch, streaming, fallback, retry,
or agent loop is added. Failure and no_action leave session state and execution
audit untouched. The [5B.2 contract and limitations](ARCHITECTURE.md#milestone-5b2-optional-one-shot-groq-provider)
describe strict decoding, fixed errors, timeouts, and owned-client cleanup.

Default dependencies remain empty. To prepare an isolated environment for offline
SDK tests, install the reviewed optional versions there (no project install needed):

```sh
python3.12 -m venv /trusted/temporary/5b2-env
/trusted/temporary/5b2-env/bin/python -m pip install 'groq==1.7.0' 'httpx==0.28.1'
/trusted/temporary/5b2-env/bin/python -B -m unittest discover -s tests -v
```

The same pins are declared as the `groq` optional extra in pyproject.toml. The
provider imports dependencies lazily; no key is needed for these tests. All SDK
tests use dummy keys, MockTransport, and blocked sockets/DNS. Historical 5B.2
verification on Python 3.12.14 with
`-B`: **270 executed/passed, 0 skipped**. With `-B -S` (no site packages):
**253 executed/passed, 17 SDK tests skipped**, 270 discovered. The prior 246 tests
remain unchanged. No live inference or real credentials were used.

Trusted callers must use the provider as a context manager or close it explicitly.
Keys are explicit host configuration, never public request data. Do not enable
SDK debug logging or expose raw exceptions; the provider refuses unsafe logging
configuration without changing application-wide logging. Transport timeouts are
finite phase limits, not a total wall-clock deadline; response size limits apply
after buffering, not while bytes arrive. Account tier/quota, actual model access,
zero-spend controls, and data-retention/ZDR settings remain unverified prerequisites
for any separately authorized live smoke test. See [decision 22](DECISIONS.md#22-milestone-5b2-optional-groq-transport-unchanged-data-only-boundary).

## Programmatic provider host (5B.3)

ProviderHost borrows the application's existing controller and adapter. It does
not construct session resources, own provider cleanup, or add a CLI mode:

```python
from controlled_agent.provider_host import ProviderHost

# Trusted application code supplies its existing per-session objects and
# public/synthetic ProviderRequest. Keep using this same host for the session.
host = ProviderHost(controller=controller, adapter=adapter)
report = host.run_once(request)  # ControllerReport, or None only for no_action.
```

Each explicit call produces once and submits non-None data once. The existing
controller still owns UUID/caller assignment and the governance/execution path.
ProviderError, terminal reports, interruptions, unexpected errors, and same-host
reentry permanently stop the host. A later call raises ProviderHostStopped with
fixed `provider_host_stopped`, not None. There is no retry, fallback, reset/resume,
resource recreation, or automatic result feedback. The owner must not bypass
stopping by replacing the host. Keep resource/provider cleanup in caller-owned
contexts and never expose raw exception diagnostics.

Nested calls raise ProviderHostReentry before another adapter/controller call.
Swallowed reentry during proposal generation prevents outer submission. After
submission begins, the original report or exception and attached report are
preserved; the original action may have completed. A nonterminal report can
therefore accompany host.stopped=True. Honor the host's stopped state and never
interpret it as cancellation or rollback. No locks or concurrency are supported.
See the [5B.3 contract](ARCHITECTURE.md#milestone-5b3-sequential-provider-to-host-integration)
and [decision 23](DECISIONS.md#23-milestone-5b3-borrow-existing-components-and-retain-host-stop-state).

5B.3 full verification on Python 3.12.14: `-B` with the existing optional
dependencies passed **292 tests, 0 skipped**; `-B -S` passed **274 tests,
18 skipped**, 292 discovered. All prior tests remain unchanged. The new host tests
include real temporary JSONL, state/handler checks, swallowed reentry, terminal
reports, preserved exceptions, and one actual-SDK/MockTransport handoff with dummy
credentials and blocked network/DNS. No live inference or credential access was
performed. Core modules, providers/adapter, scripted CLI, and dependencies are
unchanged; Scripted Demo remains the default.

## One-shot provider demo (5B.4, approved and closed)

The existing `python -m controlled_agent` scripted CLI is unchanged. From the
repository root, use the separate entry point with Python 3.12+ and a real
interactive controlling terminal. Provision an existing trusted audit parent
first; the program never creates directories or repairs log files/permissions.

```sh
# Offline by default: Mock Provider, synthetic read of WO-1001.
python3.12 -B -m controlled_agent.provider_demo --audit-path var/provider-demo.jsonl

# Offline update: the actual canonical action needs a fresh human response.
python3.12 -B -m controlled_agent.provider_demo --audit-path var/provider-demo.jsonl --scenario update

# Offline protected-target proposal: governance denies without asking approval.
python3.12 -B -m controlled_agent.provider_demo --audit-path var/provider-demo.jsonl --scenario protected-update
```

`--audit-path` is required. `--provider` accepts `mock` (default) or `groq`;
`--scenario` accepts `read` (default), `update`, or `protected-update`. No key,
free-text prompt, approval, caller, permission, retry, or model-override argument
exists. Mock needs no optional packages or network and never prompts for a key.
Logs must remain outside version control; `var/` is already ignored.

For a **separately authorized live run only**, use the same command with
`--provider groq` under an environment containing the reviewed optional packages.
It prompts for a hidden key on the controlling terminal; no saved key file or
environment credential is read. Hidden-input fallback is rejected. Presence of
SSLKEYLOGFILE, GROQ_LOG, or GROQ_CUSTOM_HEADERS, or enabled SDK/HTTP debug logging,
is refused, not silently changed. Account/model/quota/data prerequisites still
require independent review. No live inference was run for 5B.4 implementation.

Each invocation performs at most one provider call through the existing adapter,
ProviderHost, Controller and governed execution path. Read grants demo_operator
only read permission; update scenarios grant only update permission. Only public
tool descriptions and fixed synthetic prompts reach the provider. A scenario is
not an expected human response or a model-output guarantee: other structurally
valid proposals still reach governance, and review shows the actual canonical
action. Only the service interprets fresh `approve`/`decline` input. Typeahead is
discarded after complete review display/flush; EOF, partial or oversized input
never authorizes. No automatic reprompt, retry, fallback, continuation, or reset.

These are independent one-shot sessions, not the historical shared-session
three-case verifier. Fake state starts open in each process and is not durable.
Output preserves actual controller effects independently of audit/cleanup failure,
and renders only fixed notices and known demo result fields, not raw diagnostics.
It does not claim runtime audit readback verification. Exit codes: 0 for normal
completion (including denial, declined/cancelled approval or no_action), 1 for
operational/provider/terminal-host failures, 2 for usage or unavailable terminal,
130 for KeyboardInterrupt. Cleanup failure cannot erase an earlier interruption.
Output failure may prevent reporting entirely; it never triggers execution again.

New offline verification on 2026-10-25, Python 3.12.14: **25 focused tests passed**;
full discovery with Groq 1.7.0 / HTTPX 0.28.1 and `-B`: **317 passed, 0 skipped**.
With `-B -S`: **297 passed, 20 skipped**, 317 discovered. These results include
actual SDK/MockTransport, blocked network/DNS, dummy credentials, real temporary
JSONL and automated PTY input, including proof that pretyped approval is discarded.
They are not live or manual-human verification. All historical results above
remain separate. The prior unexplained live `unavailable` failure is not diagnosed
or claimed fixed by this implementation.

## Offline proposal evaluation (5C, open)

`controlled_agent.proposal_evaluation` exposes immutable `CASES`,
`score(case_id, observation)` and `assess((case_id, observation) pairs)`.
`ResponseObservation(response=None)` records actual no_action;
`FailureObservation(failure=ProviderFailure.TIMEOUT)` records failure only. No
provider is called by either scoring function. The corpus's expected answers
never enter its public ProviderRequests. Results retain only case IDs, fixed
labels, flags and counts, not raw responses or exception text.

Reuse of parse_proposal checks structure, not permission. Tool, work_order_id and
complete arguments are compared only for two proposals with a parsed observation;
comparison requires identical scalar types/values and keys, with no repair or
normalization. Otherwise those flags are None (unassessed). Malformed responses
and unexpected proposals/no_action are incorrect; all fixed provider failures
leave correctness and structure unassessed, including INVALID_OUTPUT when only
the failure code is observed. Refusal never counts as successful abstention.

Aggregation rejects duplicate/unknown IDs. It reports total observations,
unobserved case IDs, all fixed failure counts, and explicit numerator/denominator
pairs for structure, exact intent and expected no_action. Empty/partial/all-failure
batches are permitted; 0/0 means not assessed, never an invented success percentage.
A correct protected-target proposal still needs downstream governance denial;
quality results grant no authority and never enter execution JSONL.

Run the focused offline checks (no SDK required):

```sh
python3.12 -B -S -m unittest discover -s tests -p test_proposal_evaluation.py -v
```

This is **evaluator verification, not Groq quality**. The eight fixed cases do not
establish general language understanding or production readiness. Live evaluation
requires separate authorization, reviewed collection/retention, request budget,
model/account/quota/data prerequisites and caller-owned provider cleanup. The
earlier unavailable failure remains unresolved. See the
[case matrix and scoring contract](ARCHITECTURE.md#milestone-5c-offline-proposal-evaluation-open).

## Bounded proposal collection (5C, accepted after final review)

`controlled_agent.proposal_collection.collect(adapter)` returns an immutable
CollectionResult. It visits the existing eight cases in order, passing only each
case.request to the borrowed ProposalAdapter once. Expected answers and labels
stay local. There is no CLI, persistence, execution, retry, fallback or resume.

```python
from controlled_agent.mock_provider import MockProvider
from controlled_agent.provider import ProposalAdapter
from controlled_agent.proposal_collection import collect

# Offline fixture collection only. Mock has no resources to close.
result = collect(ProposalAdapter(MockProvider()))
# Eight actual no_action responses; completion does not mean eight correct answers.
```

The result holds batch (existing scoring flags/counts/unobserved IDs), status,
adapter_invocations, stopped_case_id and fixed provenance. Responses, including
no_action and mismatches, are scored and traversal continues. ProviderError only
from adapter.propose becomes a failure observation and stops the batch. A final
case failure is still provider_failure; invocation counts are not HTTP counts.
Malformed provider output becomes INVALID_OUTPUT, not no_action. Other errors
and interruptions propagate unchanged; collection_result attachment is best
effort and never replaces the original exception if building/attaching fails.
Do not print raw exceptions or tracebacks as collection diagnostics.

For caller-owned resource providers, retain the returned result or retrieve
getattr(error, "collection_result", None) before cleanup in finally. The caller
must preserve its primary exception/result and handle cleanup errors separately;
collection never calls close or supplies a lifecycle wrapper. No live use is
authorized by this example. Mock has no close method.

Current BatchResult.evidence is fixed to
`scored_observations_source_unverified`; the original scorer used
`evaluator_verification_not_groq_quality`. CollectionResult.provenance is fixed
to `adapter_collection_source_unverified`. Neither is configurable, and neither
attests live origin. SDK/MockTransport fixtures cannot become live evidence by
provider-type inspection. Future live attestation requires separate trusted
caller configuration and authorization. No model/token/cost metadata is inferred.

Results contain no raw responses, credentials or exception text, and never enter
execution JSONL or confer authority. An incomplete prefix is not whole-corpus
quality evidence. Historical test counts remain historical; the earlier
unexplained unavailable failure remains unresolved. See the
[collection contract](ARCHITECTURE.md#milestone-5c-bounded-proposal-collection-open-accepted-after-final-review).

## Tests and module boundaries

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
