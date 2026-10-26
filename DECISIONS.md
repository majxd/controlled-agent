# Architectural Decisions

This records the agreed direction for Controlled Agent's first MVP. Milestones
2A, 2B, 3A, 3B, 3C, 4A, and 4B are approved and committed. Milestone 4C end-to-end
verification passed; its documentation-only closure adds no architecture or runtime
behavior. Milestone 5A approves and documents a future LLM integration architecture
without implementing it. The [verification summary](ARCHITECTURE.md#milestone-4c-end-to-end-verification)
records the evidence and limits of the local demonstration. Earlier entries retain
the scope of their original milestone; decision 19 records the current scripted
controller/CLI integration, ADR-5A records the approved provider boundary, and
decision 21 records the authorized offline 5B.1 implementation, and decision 22
records the separately authorized optional 5B.2 Groq provider and offline verification.
Decision 23 records the authorized 5B.3 programmatic host handoff and stop policy.
Decision 24 records the separately authorized 5B.4 one-shot provider demo.

**Current closure status — 2026-10-26:** Milestone 5B is closed for the bounded
one-shot integration implemented by decisions 21–24 under ADR-5A. This is a
documentation-only acceptance review, not a new architecture decision or
implementation milestone. Earlier entries remain verbatim historical records;
their deferrals and closure statements describe those increments. Milestone 5C
remains open and unimplemented, and further live inference remains unauthorized.
The [closure summary](ARCHITECTURE.md#milestone-5b-closure--2026-10-26)
separates contract evidence, review conclusions, and continuing limitations.

The user-approved requirements take precedence over implementation convenience. Changes to scope or major architectural decisions require review; meaningful changes must be recorded here. The choice to reuse the status-update tool for the forbidden path is a small demo design choice made within the approved options.

## 1. Enforce governance deterministically outside the LLM

**Decision:** Permissions, policy evaluation, risk classification, and decision outcomes are enforced by ordinary application code. Outcomes are `ALLOW`, `DENY`, and `REQUIRE_APPROVAL`.

**Reasoning:** These rules need predictable, testable behavior. A model's interpretation or claim of permission is not authority. Governance considers the target, arguments, permissions, and policy, rather than deciding from a tool name alone. Risk does not grant permission, and a hard denial takes precedence over approval.

**Tradeoff:** The MVP supports a deliberately small rule set. It will not build a general policy language or use an LLM to decide whether its own proposals are allowed.

## 2. Separate action proposals from execution

**Decision:** The agent produces structured action proposals. The application controller submits them to governance. Only the executor invokes registered tool handlers, using authorization issued by governance.

**Reasoning:** Reasoning about an action must not confer the ability to execute it. The agent receives tool descriptions and results, not executable handlers or execution credentials. Tool definitions and argument validation alone are insufficient enforcement.

**Consequence:** Every tool path follows agent → action request → governance → authorization → executor → tool. No convenience path may bypass this sequence. Tool handlers must also remain bounded to their intended resources and effects.

## 3. Use Python 3.12+ and a single local CLI application

**Decision:** The MVP runs locally in one Python 3.12+ process with explicit module responsibilities and terminal interaction. Milestones 2A and 2B use the Python standard library; no framework or third-party dependency has been selected.

**Reasoning:** This keeps the control boundary visible without adding deployment, networking, or distributed state management. The demonstration needs only sequential local actions and in-memory approval/authorization state.

**Security boundary:** The planned architecture addresses unsafe proposals and application mistakes through its defined interfaces. It cannot contain an implementation change that adds a direct tool-call path. Module boundaries are not a security sandbox against arbitrary malicious code running inside the same process; that threat is explicitly out of scope. This learning MVP is not a production-ready system.

## 4. Issue internal, single-use execution authorization

**Decision:** Governance owns authorization state. An authorization is bound to the exact validated action, including its tool, normalized arguments, and resolved target. The executor retrieves the authorized action from trusted internal state rather than accepting a caller's assertion that an arbitrary action is approved.

**Reasoning:** A caller-supplied `approved=true` flag, a mutable action, or an authorization reusable for another target would defeat the control layer. Altering the action requires another governance decision.

**Consequence:** Missing, invalid, mismatched, pending, denied, or already-consumed authorization cannot reach a tool. Authorization is single-use and does not survive restart. Governance issues at most one authorization per action ID; a consumed authorization cannot be reissued from the same decision or approval. Milestone 2A represents authorization data with a UUID, a complete immutable action, and usable/consumed state. Milestone 3A implements trusted ALLOW-only issuance, consumption, and reference resolution, preserving the action evaluated by governance. Milestone 3B adds explicit human approval before issuance for REQUIRE_APPROVAL. Actual execution remains deferred; constructing a record still grants no authority. This decision does not require cryptographic tokens or a separate service.

## 5. Require explicit human approval for the exact sensitive action

**Decision:** The trusted CLI obtains a one-time human decision for an action classified as requiring approval. It displays the validated tool, target work order, and proposed new status. The model cannot provide approval evidence on the human's behalf.

**Reasoning:** The human must approve the actual requested effect. Approval is not a standing permission, and it cannot override a hard policy denial.

**Consequence:** A declined, cancelled, or unanswered request is not executable. An edited request must be evaluated again. Pending approvals are held only in memory and are lost on restart; recovery workflows are outside this MVP.

## 6. Keep the core domain-neutral and make the demonstration industrial

**Decision:** Core contracts and responsibilities describe actions, tools, targets, permissions, policies, risk, decisions, authorization, and audit events. Fake work orders belong to the demonstration's tool definitions, sample data, and policy rules.

**Reasoning:** An operations-oriented example makes the effects concrete without making industrial concepts prerequisites for using the governance core in another domain.

**Consequence:** The MVP never connects to a real industrial system. The governance core must not depend on work-order-specific fields or statuses; the demo supplies the domain-specific validation and policy behavior through explicit interfaces.

## 7. Demonstrate three outcomes with two tools

**Decision:** Use `get_work_order` and `update_work_order_status`. An allowed read with valid arguments and permission is `LOW` risk → `ALLOW`. A normal editable status update with valid arguments and permission is `MEDIUM` risk → `REQUIRE_APPROVAL`. Any identifiable mutation of a protected or critical work order is `HIGH` risk → `DENY`, including an attempt to close it.

**Reasoning:** Context-sensitive policy on the existing update tool proves both the approval and forbidden paths without adding another tool. Risk is derived from the action and trusted target metadata, not fixed per tool. These paths do not establish a universal risk-to-decision mapping: missing permissions still deny, and unresolvable malformed input may remain unclassified. A separate close action would add another contract and handler without materially improving this demonstration.

**Confirmed demo setup:** Keep `demo_operator`, editable `WO-1001`, and protected and critical `WO-9001`, using fake local data. The three contextual risk rules above are user-approved. [ARCHITECTURE.md](ARCHITECTURE.md) distinguishes confirmed rules from remaining sample conventions, such as permitted statuses and fixture reset behavior.

## 8. Use a fixed local caller and explicit permissions

**Decision:** Begin with trusted application-supplied caller context and a small permission allowlist. Permission enforcement remains explicit even though the MVP has no account or authentication system.

**Reasoning:** This demonstrates the difference between being permitted to request an operation and a policy permitting that particular action. It also makes missing-permission behavior testable without introducing identity infrastructure.

**Consequence:** Caller identity, permissions, risk, and resource protection metadata are not accepted as authoritative model-supplied arguments. Multi-user authentication and multi-tenant authorization are outside scope.

## 9. Audit locally and fail closed before execution

**Decision:** Use a local JSONL audit log. Assign every submitted action an action ID, including malformed submissions. Record validation/decision outcomes, approval or decline, execution start, success, and failure with that correlation ID. Denied actions remain observable even though no tool runs.

**Reasoning:** Observability must describe attempted actions as well as successful effects. If a required audit record cannot be written before execution, the action must not execute.

**Limitations:** An unavailable audit sink cannot guarantee a persisted record of its own failure; the CLI must surface that failure. A crash or logging failure after a side effect can leave an uncertain outcome. Local JSONL is not a transactional, tamper-proof, or exactly-once execution mechanism. Audit failures after execution must not be presented as proof that the side effect did not happen. [ARCHITECTURE.md](ARCHITECTURE.md) defines the conceptual events and data-minimization boundary; Milestone 4A serialization and file policy are specified in decision 18 below.

## 10. Prove the boundary with scripted proposals first

**Decision:** Build and test the deterministic control path using scripted action proposals before integrating an LLM.

**Reasoning:** Repeatable inputs isolate permission, policy, approval, authorization, executor, and audit behavior from model variability. Security-relevant tests must verify that prohibited requests do not invoke handlers, rather than merely checking displayed decisions.

**Consequence:** Scripted proposals stand in for the agent initially. The eventual model adapter must use the same action-request boundary.

## 11. Defer managed model/API selection until the boundary works

**Decision:** Remain provider-neutral now. Prefer modern managed agent/model APIs where they provide useful capabilities when model integration is considered, while keeping governance and execution control explicit in this application.

**Reasoning:** Rebuilding commodity model infrastructure is not the learning objective. Permissions, policies, execution boundaries, and observability are.

**Constraint:** A future integration must leave tool invocation under the application's authorized executor. SDK or hosted tool execution that bypasses governance, internal authorization, or the executor is excluded. No provider, SDK, model, credentials, or third-party dependency is selected in Milestone 2A.

## 12. Keep the first MVP intentionally small

**Decision:** Do not add cloud deployment, databases, queues, microservices, external infrastructure, a policy language, a vector database, multi-agent orchestration, unrestricted shell access, or unrestricted filesystem tools. Do not add automatic retries or durable approval recovery to this first prototype.

**Reasoning:** These additions would create failure modes and operational work unrelated to proving the initial control boundary. Bounded local tools and short, deterministic flows are sufficient for the three required paths.

**Consequence:** Scope expansion and meaningful architectural changes require explicit review. Documentation and focused tests accompany implementation milestones; infrastructure is not introduced without explicit approval.

## 13. Implement immutable contracts before control behavior

**Decision:** Milestone 2A adds a flat `controlled_agent/` package, a `contracts.py` module, standard-library `unittest` tests, and minimal project metadata declaring Python 3.12+ with no dependencies. There is no build-system configuration, package installation requirement, or runnable CLI. Contracts use frozen, slotted, keyword-only dataclasses and string enums. Nested JSON-compatible payload mappings are copied into read-only mapping proxies and lists into tuples.

**Reasoning:** Immutable snapshots prevent later edits to source containers from changing the recorded action, schema, or event. Structural checks make the contracts usable without prematurely implementing permission, policy, schema-evaluation, execution, or logging behavior.

**Contract review refinement:** Text metadata accepts plain strings only; string subclasses can carry mutable attributes or executable references despite having an immutable string value. Agent-facing descriptions and schemas must contain public metadata, without secrets or internal execution references. The contract does not scan or redact embedded text.

**Contract choices:** `CallerContext` contains only the caller identifier. `ActionRequest` carries a host-supplied UUID, tool name, arguments, caller, and optional target for later trusted resolution. `Decision` is only the three-outcome enum; a result containing risk and reason metadata is deferred to Milestone 2B. `Risk` has `LOW`, `MEDIUM`, and `HIGH` labels without numeric ordering. `ToolDefinition` contains descriptive metadata and an argument schema, not a handler or fixed risk. `AuditEvent` provides the documented event vocabulary, action correlation, an aware timestamp defaulting to UTC, optional caller, and details, without a sink or event-specific policy validation.

**Limits:** Constructing a structurally valid action does not mean governance has semantically validated it. UUID and caller checks do not establish provenance. Constructing an `Authorization`, even with `USABLE` state, does not establish trusted issuance or execution rights; those depend on later governance-owned state. Contract tests do not demonstrate the control boundary, and the remaining control components require subsequent authorization to implement.

The authorization identifier and state describe an internal record; neither is
proof of issuance. Future executor code must retrieve the governance-owned record
through an internal reference, never accept a supplied `Authorization` instance
as sufficient authority. No issuance or consumption behavior is added by this review.

**Focused pre-commit review:** Retain the record and state names, with explicit
documentation that `USABLE` on a supplied record is unverified, its UUID is not
automatically a registered execution reference, and construction does not check
identifier uniqueness. These are future trusted-state responsibilities. Audit
details enforce structure and immutability only; sensitive payload/result
retention and redaction remain the responsibility of later event producers.

## 14. Evaluate deterministically before implementing execution authority

**Decision:** Milestone 2B adds `GovernanceResult`, the domain-neutral
`evaluate_action` function, and separate fake work-order configuration in
`demo.py`. Internal `ToolRules` holds descriptive metadata and pure resolver,
validator, and policy functions. It contains no executable tool handler and is
not agent-facing metadata. Permissions and resource context are host-supplied
configuration; proposal fields never supply them.

**Reasoning:** Explicit domain functions are sufficient for two bounded tools.
No generic schema interpreter, policy language, authentication layer, or registry
service is needed. The core composes checks and enforces denial precedence;
domain rules determine contextual risk and proposed outcomes.

**Precedence:** Resolve a known target from arguments, then evaluate policy before
accepting arguments or permission. An identifiable protected/critical mutation
retains HIGH / DENY despite other errors. For other outcomes, target mismatch,
invalid arguments, missing permission, or unresolved risk denies the action.
Policy failures and ordinary callback exceptions fail closed. All DENY results
are terminal for their evaluation; later approval cannot override them.

**Demo conventions:** Adopt exact required arguments and no extra fields; statuses
are `open`, `in_progress`, and `closed`. Both sample orders start `open`, and an
editable same-status update still requires approval. Only `WO-1001` and `WO-9001`
resolve. Immutable metadata and permission sets remain fixed during evaluation.

**Limits:** The input must be a host-assembled `ActionRequest`. Raw dictionaries
raise `TypeError`; malformed record contents return a correlated denial. A
separate resolved snapshot leaves the original untouched. Results are data,
not authority. Pure callbacks are trusted application code, not sandboxed code.
No approval flow, authorization issuance/consumption, executor, tool handlers,
CLI, audit sink, or model integration is added. Full submission auditing and
execution-boundary tests remain for their respective milestones.

**Focused governance review:** Confirmed that target resolution must succeed and
trusted resource metadata must establish protection before the HIGH hard-deny
path applies. A supplied target is not a fallback; argument claims cannot create
or weaken trusted context. Added adversarial context tests, permission checks
across every risk and favorable policy outcome, rejection of authority fields
on results, and file-I/O/human-input guards. No governance behavior or architecture
changed in this review; approval and execution enforcement remain deferred.

## 15. Keep issuance provenance and one-use state inside a trusted service

**Decision:** Milestone 3A introduces one sequential, non-reentrant
`AuthorizationService` per application run. It captures trusted configuration,
calls governance internally, and retains the exact validated/resolved action.
The public result-only governance API remains compatible. Neither imported
evaluation results nor constructed Authorization records can be supplied for
issuance. DENY and REQUIRE_APPROVAL never issue references in this increment.

**Identity and lifetime:** Internal opaque objects identify registry entries by
identity. Authorization UUIDs remain auditable record identifiers and are rejected
as execution references. Copies, forged records, cross-registry references, and
replays fail. Consumption marks the private entry consumed before any replacement
record is constructed or the action is returned. The future executor supplies
only the reference, never replacement arguments. No reference enters audit or
agent-facing data. The host must use only one live service as its issuance authority.

**Failure rules:** Reserve action IDs before external audit/policy callbacks and
never release them during the run. Repeated IDs are terminal even after failure
or denial. Submission, decision, and issuance audit writes are mandatory; no
optional/default no-op writer exists. Publish only after issuance audit succeeds.
Any exception or interruption before issuance returns removes partial publication
without permitting reissuance. Reentrant calls abort the outer operation even if
their immediate exception is caught. Single-thread sequential use remains a
requirement; this is not a parallel dispatcher or a hostile-code sandbox.

**Audit boundary:** Require a synchronous writer that returns None after required
write/flush, or raises. Only test recording/failing writers are implemented.
Rejected consumption is audited; accepted consumption is not execution and emits
no execution-start event. The later executor must write execution start after
consumption and before dispatch. A successful audit entry cannot recreate rights;
failure after an issuance log but before publication can leave a log with no live
reference. Logging failure after future dispatch must report actual/uncertain
outcome separately, without retries or claims of rollback.

**Safe retention:** Submission events label the claimed selection of a registered
tool; decision events retain that known tool and the safely resolved target even
on DENY or REQUIRE_APPROVAL. The target comes from the original evaluation, with
no second resolution. A separate `action_validated` flag records whether all
validation/permission gates passed; neither the flag nor any audit data grants
authority. Public configured tool/target identifiers are retained only when they
use 1–128 ASCII letters, digits, underscores, periods, colons, or hyphens. Unknown
claims and out-of-policy identifiers are omitted, not truncated. Host configuration
must keep retained identifiers and reason codes non-sensitive.

No argument payload is logged, including on issuance: semantic validation is not
a secrecy review. Exact action binding remains in the private registry. The audit
trail identifies attempts and outcomes but cannot reconstruct full arguments;
future payload/result retention requires an explicit safe field policy. This
resolves the non-issued-action audit context gap without adding authority to logs
or implementing a concrete sink.

**Deferred in Milestone 3A:** Human approval and its explicit display/response binding, raw
submission parsing, concrete JSONL storage, executor, handlers, CLI, and LLM
integration. Tombstones and consumed entries stay in memory until exit; no
garbage-collection policy or durable rights recovery is introduced.

## 16. Bind one synchronous human review to the retained action

**Decision:** Milestone 3B extends the existing service with optional construction-time
`approval_formatter` and `HumanApprovalAdapter` dependencies. Both must be supplied
together. REQUIRE_APPROVAL then creates private pending state referencing the exact
validated/resolved action and its governance result. Without configuration, 3A
behavior remains unchanged. DENY never creates pending state, prompts, or issues.

**Presentation:** The service creates immutable `ApprovalReview` data containing
the canonical action. A trusted formatter returns only immutable current/proposed
state and intended effect. Work-order presentation stays in `demo.py`, deriving
statuses from canonical arguments and the same trusted resource snapshot used by
governance. It ignores proposal-authored display text. The trusted display callback
must present action ID, caller, tool, target, and exact change before the reader
obtains one fresh human response. There is no concrete console adapter in 3B.

**Response and failure boundary:** Only a plain string `approve`, allowing surrounding
whitespace, affirms. `decline` refuses; empty/None/other values and EOF cancel. Errors,
interruptions, and reentrancy never authorize. There is no public approval endpoint
or externally usable pending token. Pending state closes irreversibly before result
auditing and clears on every exit, while action IDs remain reserved. Changed actions
need a fresh submission. Responses cannot carry replacement action fields or reusable
approval records. The original REQUIRE_APPROVAL decision remains in the result tuple
even when a separate execution reference is successfully issued.

**Audit gates:** `approval_requested` must succeed before review callbacks. After
closing pending state, write sanitized `approval_result`; only an affirmative result
with successful auditing proceeds to `authorization_issued` and registry publication.
All existing rollback and single-use guarantees remain. Approval audit details retain
only bounded tool/target identifiers and fixed outcome/reason codes, never arguments,
review text, raw responses, or exceptions. Failure can leave incomplete logs; no
recursive audit retries or replay of audit data is allowed.

**Tradeoffs and limits:** Synchronous review keeps the original service operation
guard active and avoids asynchronous handles, expiry, and recovery machinery. Callback
code is trusted to present faithfully and collect human input; authentication and
verification that a person actually saw the display are not provided. Current-state
display uses a fixed snapshot, not live data: freshness and external-state TOCTOU
protection remain deferred. No executor, handlers, CLI, concrete sink, LLM, concurrency,
persistence, or dependency is added.

## 17. Consume once, audit before dispatch, and preserve actual effects on failure

**Decision:** Milestone 3C adds a domain-neutral Executor and a separate
FakeWorkOrderTools fixture. The executor accepts only a private service reference,
consumes it irreversibly, retrieves the exact governed action, and successfully
writes execution_started before invoking its fixed trusted handler. Handler mappings
are copied/read-only and separate from descriptive tool metadata. Missing handlers
and all pre-dispatch failures leave authorization consumed.

**Integration:** A private service execution scope shares the original guard across
consumption, audit callbacks, dispatch, and completion. Public consume() remains
compatible. Reentrant service/executor calls poison the outer operation even if
caught; before dispatch they prevent invocation, after dispatch they preserve the
known/uncertain effects and prohibit further callbacks. No recursive audit attempts.

**Outcomes:** Immutable host reports distinguish no dispatch, completed handlers,
trusted rejection without mutation, and possibly partial effects. ExecutionError
preserves outcome and separate audit/handler diagnostics. Interruptions propagate
with execution_report attached. A failed result audit does not undo a successful
tool call. No automatic retries, rollback claims, or restored authority exist.
ToolRejected is reserved for trusted handlers that have performed no mutation.
Audit events retain bounded identifiers and fixed codes, never arguments, results,
raw errors, references, review text, or human responses. Actual durability depends
on the injected synchronous writer; no concrete sink is added.

**Demo baseline tradeoff:** One per-run fixture uses fixed sample protection bounds
and captures the same authorization service's immutable review baseline. Handlers
independently enforce known IDs, exact fields/target consistency, allowed statuses,
and protected/critical mutation denial. Immediately before mutation, live status
must equal the fixed reviewed baseline. A stale mutation fails without modification
and its reference stays consumed. After a status-changing update, subsequent mutations
are rejected until a new run; reads and baseline-matching same-status updates work.
No service/fixture recreation within a run may bypass that limitation. Display remains
a fixed snapshot, not live current state; refresh and external TOCTOU protection are
deferred rather than silently changing the approved transition.

**Limits:** Host wiring must use one fixture/executor with the same service. Handler
code and configuration are trusted; Python privacy cannot prevent arbitrary direct
calls or malicious mutation. At-most-once dispatch does not provide exactly-once
side effects. CLI, raw proposal parsing, concrete human I/O, concrete audit sink,
LLM integration, concurrency, persistence, and new dependencies remain deferred.


## 18. Strict local JSONL persistence with mandatory per-event fsync

**Decision:** Milestone 4A adds only `audit.py`, persistence/integration tests, and
documentation. `JsonlAuditWriter(path)` is a context-managed synchronous callable
compatible with the existing service. No authorization, approval, governance,
executor, tool, or contract production behavior changes.

Every event is explicitly encoded as a compact UTF-8 JSON object with one newline:
action ID, UTC ISO timestamp, event type, public caller ID or null, and exact
per-event allowlisted details. The architecture records every allowed shape,
including both execution-blocked variants. Unknown/missing fields, unsupported
values, nested payloads/tuples, cycles, nonfinite numbers, invalid identifiers,
and records larger than 4096 encoded bytes including LF are rejected. Current
shapes need only flat scalar fields. Public identifiers and reason codes have
explicit 128-character ASCII bounds. No `default=str`, silent redaction, or
argument/result retention is permitted. A valid identifier can still contain a
secret; trusted configuration must keep retained strings public.

**File policy:** The host provisions the existing trusted parent and supplies the
path. The initial implementation requires POSIX O_NOFOLLOW. Append/create opening
and descriptor checks reject final symlinks, nonregular files, foreign owners,
and any mode other than 0600. New files request 0600 subject to umask; no chmod
repair. Nonblocking open prevents FIFO hangs before rejection. A nonempty file
must end in newline, but its history is not parsed or authenticated. Ancestors,
ACLs, exclusive access, and safe path selection are host responsibilities.

**Acknowledgement/failure:** Encode/validate fully before per-event I/O; write
unbuffered bytes once, verify the complete count, flush, and os.fsync for every
record. There is no configurable weaker policy. Any event failure permanently
poisons that instance, including serialization and interruption; no automatic
retry/reopen/repair or logging of failure through the same sink. Complete visible
records after sync failure do not imply successful issuance or execution. Close
runs once; an earlier failure/interruption is preserved if cleanup also fails,
with a fixed note. Raw OS causes are private host diagnostics, never log details.

**Reasoning and limits:** A fixed fsync gate makes persistence acknowledgement
explicit at modest local-MVP cost. It is not an absolute power-loss guarantee and
does not sync the parent directory entry. JSONL is neither tamper-resistant nor
transactional with handler effects. It cannot prove historical completeness or
provide exactly-once recovery. Failed completion logging does not undo effects;
the existing executor reports known/uncertain outcomes without retries. No log
record, UUID, or approval label recreates registry rights. No locking, rotation,
concurrent writers, rights persistence, CLI/controller, model integration, or
third-party dependency is added. Runtime files stay in already ignored directories.


## 19. One service-owned admission boundary and one CLI application lifetime

**Decision:** Milestone 4B adds a bounded pure proposal parser, thin controller,
terminal CLI, and module entry point. The demo layer supplies three scripted data
proposals without receiving execution components. The controller generates a UUID
before parsing and supplies its fixed caller. Only the executor invokes handlers.

The host-only issue_proposal API shares admission, ID tombstones, reentrancy guard,
governance, approval, issuance, and cleanup with issue(ActionRequest). Submission
audit precedes parsing and therefore has a null tool claim. Malformed proposals
receive correlated DENY/MALFORMED_PROPOSAL with no validated action or fabricated
risk/target. Parser limits: exact plain tool_name/arguments envelope, tool/key length
128, eight flat scalar arguments, string values 256, signed 64-bit integers, finite
floats, and compact UTF-8 proposal size 4096 bytes. Bounds do not validate semantics;
governance still decides permission, target context, risk, and hard denial.

**Reasoning:** A second controller admission registry or placeholder ActionRequest
would weaken correlation and terminal failure semantics. Shared service admission
keeps both APIs compatible and prevents reissuance across them. No audit sink,
contract, governance, executor, or handler policy changes are required. The controller
returns no reference and emits no duplicate lifecycle audits.

**Terminal policy:** One writer/service/fixture/executor per session with demo_operator
and fixed configuration. Existing trusted audit parent required; optional audit path
is host startup configuration only. Real startup requires interactive terminal input
and output; test hosts inject streams. Flush the canonical fixed-baseline review
before one fresh response. Only the existing approve rule issues. Reject lines over
256 content characters and unterminated EOF lines rather than authorizing a prefix.
Invalid menu choices create no action. No auto-approve option or default response.

**Results/failures:** Fixed outcome messages separate denied, malformed, approval
not granted, succeeded, rejected without mutation, blocked, uncertain effects, and
audit failure. Decline/cancellation remain grouped because the existing API groups
them; the audit is more specific. Raw exceptions/references are never output. Only
known demo result ID/status fields are rendered; tool output remains untrusted.
EOF cancels approval and ends the session. KeyboardInterrupt exits 130 after existing
cleanup/reporting; SystemExit from session callbacks is sanitized as interruption
and exits 1. Operational failures exit 1, usage/noninteractive errors 2, normal
exit 0. Pre-dispatch audit failure prevents effects, while post-dispatch failures
retain completed/uncertain effects. Close errors never imply rollback. No retry.

**Limitations:** Terminal access is not human authentication or proof of viewing.
Review remains a fixed snapshot and later updates can become stale; no reset or
refresh is introduced. Security remains an application interface boundary in one
trusted sequential process. No model, network, concurrent processing, new dependencies,
or persistence beyond JSONL. Runtime logs and the four unrelated presentation
artifacts stay out of version control. All 184 prior tests remain intact.

## 20. ADR-5A: Defer provider integration behind the existing controller boundary

**Decision:** Milestone 5A approves the architecture for a future,
provider-independent proposal adapter; it adds no runtime code, dependencies,
provider credentials, API calls, CLI changes, or tests. The Scripted Demo remains
the default proposal producer. A future adapter may use an offline Mock Provider
and may optionally support one managed API provider, but every action-shaped result
must enter only through the existing `Controller.submit(proposal)` boundary. The
adapter must preserve the strict plain `tool_name`/`arguments` proposal envelope;
it must not create a new trusted `ProposedAction` or `ActionRequest` authority.

**Trust boundary:** Model and provider output are untrusted data. The adapter may
provide public tool metadata and permitted synthetic industrial data to a provider,
then return either `no_action` or a plain proposal to the controller. It may not
issue approvals, authorize execution, invoke handlers, choose the trusted caller,
select policy/risk/permissions, access internal authorization references, or bypass
governance, human review, the executor, or the existing audit gates. Validly
structured proposals for unknown tools or protected targets still reach existing
deterministic governance and are rejected there. The controller remains responsible
for its fresh host action ID, fixed caller, admission audit, parsing, governance,
authorization lifecycle, and immediate executor handoff.

**Failure and lifecycle rules:** An adapter-level `no_action` produces neither a
controller submission nor a fabricated tool call. Invalid provider output, refusal,
timeout, rate limit, and provider failure stop before submission; they do not create
an action ID, authorization, execution audit event, or retry. There is no automatic
provider fallback and no automatic retry, especially after any proposal has been
submitted. One sequential session continues to own the same writer, authorization
service, fixture, executor, and controller for its entire lifetime. Provider errors
must not reset or recreate trusted state, clear tombstones, restore consumed
references, or retry an uncertain effect. Execution JSONL remains an execution
audit only; provider metrics and model-quality evaluation must be separate.

**Evaluation posture:** Groq Free and GPT-OSS 20B are initial experimental
candidates, not provider or production commitments. The current experimental budget
is zero. Before any live managed API use, future work must validate actual quota,
billing, privacy, and model support. Only synthetic industrial data and public tool
metadata may be sent to a provider. Secrets, audit logs, private policy
configuration, authorization references, and other internal authority must not be
sent.

**Alternatives considered:** Direct SDK or hosted-tool dispatch was rejected because
it could bypass the control boundary. Giving a provider trusted action records,
approval authority, or caller selection was rejected because it confuses untrusted
proposal data with host authority. Adding automatic fallback/retry was rejected
because it obscures provider failure and can repeat a submitted or uncertain effect.
Selecting a managed provider now was rejected because no live budget, credentials,
or provider assurance has been approved. Keeping only a scripted producer was also
rejected as the long-term architecture because it would not specify how future
provider proposals preserve the validated boundary.

**Limitations and implementation deferral:** This decision does not establish
commercial viability, production readiness, provider safety, quota availability,
privacy compliance, or model quality. It introduces no Agent SDK, cloud deployment,
industrial integration, new tool, dashboard, database, autonomous loop, or model
selection. The remaining 5B scope and Milestone 5C require separate authorization
for managed-provider work and model-quality evaluation. Offline lifecycle and
policy-bypass coverage is included in 5B.1 below. The earlier 228 passing tests
are a baseline, not verification evidence for future integration.

## 21. Milestone 5B.1: Keep the offline provider interface data-only

**Decision:** Implement an immutable ProviderRequest containing a bounded
public/synthetic prompt and existing public ToolDefinition metadata, a synchronous
ProposalProvider protocol, a structural ProposalAdapter, and a simple offline
MockProvider. Reuse parse_proposal to return a copied plain tool_name/arguments
envelope. None alone represents no_action; the string "no_action" is invalid.
Fixed ProviderFailure codes distinguish invalid output, refusal, timeout, rate
limit, and unavailability. Ordinary unexpected errors are sanitized; interruptions
propagate. Neither request data nor failure codes grant authority.

**Reconciliation rationale:** The adapter receives no controller or execution state.
Only the trusted host submits a non-None result once through its existing
Controller.submit and handles terminal reports. This keeps proposal production
independent of orchestration without changing the validated core. Unknown tools,
targets, argument claims, permissions, risk, and approval remain with governance.
The basic mock stays stateless between calls apart from its configured value;
test-only sequential providers verify unused responses after failure. A separate
mock module is an organizational choice, not a security guarantee.

**Consequences and evidence:** No-action and provider failures create no controller
submission, action ID, or execution lifecycle audit. Real temporary JSONL integration
tests read persisted records and check correlation/order, handler counts, actual
state, admission state, approval binding, denial, sanitized errors, and interruption.
Failure after mutation preserves consumed authority and stale state without retry,
fallback, or session reset. This remains an interface boundary within trusted
Python code, not a sandbox or proof of production readiness.

The reconciled full suite passed 246 tests on Python 3.12.14 with bytecode writes
disabled on 2026-10-18, including 18 provider tests and the unchanged baseline.

**Historical scope:** 5B.1 alone was authorized for this increment. Core production modules, Scripted Demo, and
CLI behavior are unchanged. No live API, Groq integration, dependency, credentials,
autonomous loop, provider metrics, or model-quality evaluation is added. Live
integration, transport timeouts, conversation history, and result feedback remain
deferred. ADR-5A's historical design approval did not authorize these later items.

## 22. Milestone 5B.2: Optional Groq transport, unchanged data-only boundary

**Decision:** Add one context-managed synchronous GroqProvider, its offline tests,
and a pinned optional extra (`groq==1.7.0`, `httpx==0.28.1`). Default dependencies
remain empty. The official SDK provides maintained request, error, timeout, and
cleanup interfaces without adding agent execution machinery. Dependency imports
are lazy; the Scripted Demo and Mock Provider remain dependency-free and unchanged.
The experimental candidate is openai/gpt-oss-20b, not a production selection.

**Contract:** Preserve ProviderRequest, ProposalProvider, ProposalAdapter, and
Controller.submit without modification. Use one JSON-mode non-streaming completion,
no registered tools, `tool_choice="none"`, `include_reasoning=False`,
`reasoning_effort="low"`, and `max_completion_tokens=2048`; omit reasoning_format.
Only exact proposal/no_action/refusal wire variants are accepted. Explicit wire
no_action alone becomes None. Missing content, malformed/truncated output, or
refusal is never silently converted to no_action. The trusted host alone submits
a copied plain proposal; governance still owns all semantic and authority decisions.

**Rationale and tradeoff:** JSON object mode avoids a dynamic provider-specific
schema compiler or tool allowlist that would duplicate governance. It does not
promise schema compliance, so strict local parsing remains mandatory. Groq also
offers strict structured outputs for the candidate, but schema construction is
not necessary for this bounded increment. No output repair or retry compensates
for model errors. See [Groq structured outputs](https://console.groq.com/docs/structured-outputs)
and [reasoning options](https://console.groq.com/docs/reasoning).

**Transport and decoding:** Use only the documented raw-response interface and
public read/close methods from the [official SDK](https://github.com/groq/groq-python).
Both JSON layers reject duplicate keys, invalid Unicode, nonfinite numbers, deep
nesting, prose, and trailing data. API null/empty metadata is accepted where harmless;
actual tool calls are rejected. Exact-key rules apply to model-authored variants
and proposals. Fixed failure codes replace raw diagnostics. SDK and transport
retries are explicitly zero; redirects and ambient proxies are disabled. Finite
phase timeouts are not a total deadline, and size limits apply only after buffering.
These limits and exact mapping rules are recorded in the 5B.2 architecture section.

**Secrets and ownership:** Trusted construction supplies the key, never a prompt,
ProviderRequest, result, audit record, or user diagnostic. The provider owns and
closes its SDK/HTTP client, including on failures; cleanup does not replace an
existing interruption. It neither logs payloads nor changes application logging
configuration. Unsafe SDK logging/custom-header environment settings and enabled
debug loggers are refused; host configuration must remain fixed during a call.
This protects the defined interface, not against arbitrary in-process code.

**Evidence:** The unchanged 246-test baseline plus 24 new tests passed on Python
3.12.14 with -B: 270 executed, zero skipped. Without site packages, 253 executed
and passed and 17 SDK tests were skipped. Actual SDK/MockTransport tests block
network/DNS, use dummy keys, assert one request and no retries, failure mapping,
cleanup, strict decoding, and dependency-free operation. Persisted JSONL tests
assert correlation, lifecycle, handler counts, state, admission state, consumed
authority, and unused queued success after failure. No-action/failure never submits.

**Remaining gate:** Live inference was neither performed nor authorized by this
increment. A later zero-budget smoke test needs separate authorization and verified
account tier/quota/spend controls, model access, and data settings. Check the actual
account rather than assuming the public [rate-limit table](https://console.groq.com/docs/rate-limits)
applies. Review [Groq data controls](https://console.groq.com/docs/your-data), including
retention exceptions, ZDR settings, and retained usage metadata. Public/synthetic
inputs only. No CLI expansion, hosted tools, streaming, agent loop, fallback,
execution-core changes, model-quality claim, or production-readiness claim is made.

## 23. Milestone 5B.3: Borrow existing components and retain host stop state

**Decision:** Add a small ProviderHost with run_once(ProviderRequest) returning
the existing ControllerReport unchanged or None for no_action. It accepts the
existing Controller and ProposalAdapter, calls the adapter once per explicit
invocation, and submits a non-None proposal once. It neither produces authority
nor changes the provider, adapter, core, CLI, or dependencies. The caller owns the
same session resources and provider cleanup throughout; no reconstruction occurs.

**Why a wrapper:** Existing tests already demonstrated the handoff, but no runtime
host component retained stop state. ControllerReport.terminal is a signal to the
host, not a latch inside Controller. A stateless helper would leave every caller
to reinvent that lifecycle check. No new result record, registry, dispatcher,
provider abstraction, SDK import, or resource factory is needed.

**Conservative lifetime policy:** ProviderError, terminal reports, interruptions,
unexpected errors, and same-host reentry permanently stop the wrapper. Calls after
stopping raise dedicated ProviderHostStopped(`provider_host_stopped`), never None.
No-action and nonterminal reports permit another explicit call, not an automatic
loop or retry. Provider-error stopping applies only to this host; the adapter's
existing contract and tests are unchanged. No reset/resume, fallback, or replacing
the stopped wrapper to recover a failed session is supported.

**Reentry and effects:** Reject a nested run_once before invoking either collaborator
and latch stopped, using ProviderHostReentry(`provider_host_reentry`). After proposal
production, recheck stopped before submission or returning None, even if the
provider swallowed the rejection. An uncaught nested error can instead emerge
as the adapter's existing sanitized ProviderError. Once controller submission has
started, preserve the actual report or original exception with its attached report.
A swallowed callback rejection may allow the original authorized action to finish;
return that report unchanged and keep the host stopped even if report.terminal is
False. This does not cancel the core, restore consumed authority, or roll back
effects. The existing service/executor guards are not modified or replaced.

**Scope and evidence:** Seven files only: provider_host.py, its new test module,
and the five project documents. Existing tests remain unchanged. The full suite
on Python 3.12.14 with -B passed 292 tests with zero skipped, using the unchanged
Groq 1.7.0 / HTTPX 0.28.1 environment. With -B -S, 274 passed and 18 SDK tests
were skipped. New tests focus on handoff counts, stop/reentry behavior, unchanged
reports/exceptions, real JSONL correlation and lifecycle, actual handler/state
effects, retained admission/consumption state, and unused queued responses. One
optional SDK/MockTransport integration blocks network/DNS and uses a dummy key.
No live inference or credential access was performed. The earlier user-reported
manual connectivity/decoding smoke test is separate, not end-to-end live evidence.

**Limitations:** Sequential use only; no locks or concurrency support. Stop state
is application lifecycle data, not authorization or a sandbox against in-process
bypasses. Programmatic integration adds no CLI mode, prompt/key input, rendering,
automatic feedback, metrics, or agent loop. Scripted Demo remains the default;
further live execution and any provider CLI changes need separate approval.

## 24. Milestone 5B.4: Separate one-shot terminal wiring, Mock by default

**Decision:** Add provider_demo.py and focused tests, with a separate module entry
point and a required trusted audit path. Keep the scripted CLI, providers, adapter,
ProviderHost, controller, execution core and dependency declarations unchanged.
Default Mock/read needs no network or SDK. Fixed synthetic read/update/protected-update
scenarios select public metadata and narrow host-configured permissions, not policy
decisions or human answers. Explicit Groq selection requires hidden terminal key
input; no ambient/saved credentials, free prompts or model overrides are supported.

**Rationale:** A small runner and terminal adapter are sufficient. Reuse the existing
scripted proposal data, canonical domain formatter and safe Terminal report renderer;
do not reuse the scripted menu loop or create another provider/session framework.
Do not migrate temporary smoke-test instrumentation, private-state verification,
HTTP observers or exact-output guards. Unknown or alternate structurally valid
proposals still reach deterministic governance through the unchanged host path.

**Lifetime and reporting:** One session, one run_once, no retries, fallback,
continuation or reset. The caller closes the owned Groq provider and audit writer;
MockProvider has no close interface or resource ownership. Retain original reports
and attached controller_report before cleanup. Keep reporting channels open until
operational cleanup is known; report cleanup/output failure separately without
erasing completed/uncertain effects or the original interruption. Honor host.stopped
even with nonterminal success. Exit 0 is normal handling, not proof of execution;
1 is operational/provider/terminal-host failure, 2 is usage/terminal unavailability,
and KeyboardInterrupt is 130. No runtime audit-readback verification is claimed.

**Input boundary:** Refuse SSLKEYLOGFILE presence before SDK/client construction;
preserve existing unsafe logging/header guards without reading values or changing
configuration. Reject GetPassWarning instead of accepting echoed key input. Display
the complete bounded canonical action, flush, discard queued terminal input, and
read one fresh bounded newline-terminated response with no buffered read-ahead.
Only the existing service interprets approval. No expected-response coercion,
truncation-to-approval, reprompt or human-authentication claim is introduced.

**Evidence and limits:** New 2026-10-25 offline verification on Python 3.12.14:
25 focused tests passed; full `-B` discovery with Groq 1.7.0 / HTTPX 0.28.1 passed
317 tests, 0 skipped. Without site packages (`-B -S`), 297 passed and 20 skipped.
Actual SDK tests use dummy keys, MockTransport and blocked network/DNS. Real JSONL
and PTY tests verify lifecycles, effects, cleanup and pretyped-approval rejection.
Historical sections remain unchanged. Final user review approved and closed 5B.4 only;
no live inference, broader model-quality claim or further milestone closure follows.
