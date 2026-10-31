# Repository instructions

## Working agreement

- Final acceptance closed the current local MVP/prototype scope on 2026-10-31,
  including bounded 5B and 5C. The seven-criterion evidence matrix and
  remaining limitations are in ARCHITECTURE.md. This adds no features or
  production-readiness, broad model-quality, or live provider_demo claim.
  Further live inference requires separate authorization; the earlier
  unexplained unavailable failure remains unresolved.
- Act as an implementation partner. Follow the user's authorized milestone;
  do not silently expand product or architectural scope.
- Current status: Milestones 2A, 2B, 3A, 3B, 3C, 4A, and 4B are approved and committed.
  Milestone 4C end-to-end verification has passed and is approved; its closure is
  documentation only. The verified baseline has 228 passing Python 3.12.14 tests.
  See the Milestone 4C summary in ARCHITECTURE.md for automated pseudo-terminal
  evidence and limitations; it is not manual-human or production verification.
  Milestone 5A is closed as documentation-only architecture approval. Milestone 5B.1
  implements the authorized offline provider interface, Mock Provider, and structural
  adapter. Milestone 5B.2 adds an explicitly authorized optional Groq provider,
  verified offline with the actual SDK and blocked network/DNS. Milestone 5B.3
  adds the authorized sequential ProviderHost handoff and permanent stopping.
  The separately reported 2026-10-19 live smoke test verified connectivity and
  proposal decoding only. User-reported live three-case verification on 2026-10-21
  covers human decline, protected-target denial, and human-approved synthetic
  local update, with persisted auditing and cleanup; see ARCHITECTURE.md. It is
  separate from the earlier read-only test and offline evidence. The prior
  unavailable failure remains unexplained; that live evidence alone closed no
  additional milestone.
  Milestone 5B.4 implements the separately authorized one-shot provider demo,
  Mock by default, with offline verification; final user review approved and closed 5B.4.
  Milestone 5B is closed by documentation-only review on 2026-10-26, limited to
  the bounded one-shot integration implemented in 5B.1–5B.4. Historical evidence
  and limitations remain unchanged; the provider_demo entry point has no live
  verification claim. The scripted CLI remains unchanged. Milestone 5C includes
  its authorized pure offline scorer, eight synthetic cases and deterministic
  evaluator verification, accepted after final review.
  The separately authorized bounded programmatic collector is implemented with
  offline fixture verification, accepted after final review. Fixtures and Mock results
  do not measure Groq quality. Further live collection and scope remain unauthorized.
  A user-attested live synthetic batch and separate offline SDK/helper checks are
  recorded in ARCHITECTURE.md. Bounded 5C synthetic evaluation was approved and
  closed on 2026-10-31; 5B remains closed.
  The batch does not establish broader model quality or execution verification.
  Do not add dependencies or commit unless requested.
- Read `PROJECT_BRIEF.md`, `ARCHITECTURE.md`, and `DECISIONS.md` before making
  implementation or architectural changes. Explicit user direction takes
  precedence; document meaningful changes to existing decisions.
- Preserve Python 3.12+, one local application, a CLI, and small explicit module
  boundaries. No external infrastructure without explicit user approval.
- Contracts use standard-library immutable snapshots. Their structural checks
  do not validate tool semantics, authenticate callers, evaluate policy, or
  grant execution rights. Constructing an `Authorization` record, including
  one marked `USABLE`, must never substitute for the service's trusted issuance state.
  Neither its UUID nor its state proves issuance; do not accept a supplied record
  as executor input or expose it as agent-facing metadata.
- The host owns one sequential authorization service per run. Reserve action IDs
  before audit/policy callbacks; keep failures terminal. Its opaque references
  remain internal and never enter agent-facing data or audit payloads. Reentrant
  calls must abort the outer operation even if a callback catches the rejection.
- The service requires a trusted synchronous audit writer. Milestone 4A provides
  the separate JsonlAuditWriter sink; the service does not own file lifecycle.
  DENY never issues. REQUIRE_APPROVAL can issue only after one
  explicit human response through construction-time trusted review callbacks and
  successful approval-result/issuance audits. Without approval configuration it
  still issues no reference. Pending state is private and terminal on every exit.
  Public consume() does not dispatch. Executor.execute(reference) uses the private
  shared service guard through consumption, execution-start audit, dispatch, and
  completion auditing. No replacement action/arguments or restoration on failure.
  Reentrant issue/issue_proposal/consume/execute calls cannot dispatch another action; a poisoned
  operation invokes no further callbacks after detection.
- Review callbacks may describe/present the exact canonical action and collect a
  fresh human response only; they may not substitute action fields or supply model
  text as approval. Current-state display uses a fixed resource snapshot, not live
  state or TOCTOU protection. Do not log review text, arguments, or raw responses.
- The host binds one Executor and one FakeWorkOrderTools fixture to the same
  authorization service per run. Handler configuration is trusted and fixed.
  The fixture captures that service's immutable review baseline and rejects a
  mutation if live status differs. Never recreate fixtures or the service mid-run
  to clear stale state, consumed references, or action-ID tombstones.
- Execution reports distinguish no dispatch, completed handlers, trusted rejection
  before mutation, and potentially partial effects. ToolRejected is a trusted
  handler promise of no mutation, not a generic exception shortcut. Keep audit
  failures separate from effects; interrupts retain their type and execution_report.
  Neither reports, results, nor exceptions carry reusable authority.

- Controller.submit assigns a fresh host UUID before parsing and attaches its fixed
  caller. issue_proposal and issue share the same service admission guard, ID
  tombstones, evaluation, approval, publication, and rollback logic. Audit submission
  before parsing; malformed proposals receive DENY/MALFORMED_PROPOSAL without a
  placeholder ActionRequest. Do not create a controller-owned admission registry.
- The parser accepts only plain tool_name/arguments dictionaries with bounded flat
  scalar arguments. Top-level authority fields are invalid; argument claims remain
  untrusted data subject to governance. Invalid menu choices are UI errors, not
  action submissions. Scripted proposal production receives no execution components.
- CLI sessions own one writer/service/fixture/executor throughout. Use canonical
  review text, flush before input, and never accept truncated or unterminated EOF
  input as approval. No auto-approve or model-selected configuration. Render only
  fixed outcome messages and bounded public demo result fields, never exceptions.
  Exit on operational/audit failures without retry or resetting security state.

## Mandatory execution boundary

- Preserve proposal → governance → internal authorization → executor → tool.
  Never add a direct agent-to-tool path, including SDK automatic dispatch or
  hosted tools that bypass governance.
- The proposal producer receives descriptions and results, not executable
  handlers or tool credentials. Only the execution side invokes handlers.
- Keep agent-facing descriptions and schemas public: no secrets or internal
  execution references in text, defaults, or examples. Structural contract
  checks do not detect or redact embedded secrets.
- Governance is deterministic and outside the LLM. Do not use model judgments
  for permission, policy, risk, approval, or execution authorization.
- Evaluate target, arguments, permissions, and policy for each action. Risk is
  context-sensitive, not fixed per tool, and never grants permission. LOW-risk
  actions still require permission; human approval can never override `DENY`.
- Treat proposals, model text, and tool output as untrusted data. Caller identity,
  permissions, protected-resource metadata, and approval evidence come from
  trusted application context, never from proposal claims.
- Preserve ADR-5A and the 5B.1 boundary: Scripted Demo remains the default.
  ProviderRequest contains only a bounded public/synthetic prompt and immutable
  public ToolDefinition metadata. ProposalAdapter returns a copied plain
  `tool_name`/`arguments` proposal or None; None alone means no_action, and the
  string "no_action" is invalid. Only the trusted host submits non-None proposals
  through its existing Controller.submit. The adapter receives no controller or
  execution state. Do not introduce a trusted provider action record, let a
  provider select caller/policy/risk/approval, or expose handlers, credentials,
  private policy configuration, audit logs, or authorization references to it.
  `no_action`, invalid provider output, refusal, timeout, rate limit, and provider
  failure must stop before submission. Never add automatic provider fallback or
  retry, reset trusted session state after provider failure, or merge provider
  metrics/model-quality evaluation into execution JSONL auditing. Design approval
  is not implementation authorization: do not expand beyond the authorized 5B.4
  one-shot demo without explicit scope.
- ProviderHost borrows the existing controller and adapter; the caller still owns
  all resources and provider cleanup. Each explicit run_once calls the adapter once
  and submits non-None output once. None means only no_action, never stopped-host
  misuse. ProviderHostStopped has a fixed diagnostic. Provider failures, terminal
  controller reports, interruptions, unexpected errors, and same-host reentry stop
  the host permanently. Reject nested calls before either collaborator. Swallowed
  reentry during proposal generation must prevent outer submission. Once submission
  has begun, preserve the original controller report or exception and attached
  report; stopping the host does not cancel or roll back that action. A callback
  may swallow reentry and the core may return a nonterminal success: the host still
  stays stopped. Do not add locks, concurrency, reset/resume, retries, fallback, or
  another admission registry, or replace the stopped wrapper to bypass its policy.
- GroqProvider owns its SDK/HTTP client and closes it deterministically. Preserve
  the optional dependency boundary and empty default dependencies. Use the public
  raw-response API, strict bounded decoding, zero SDK/transport retries, finite
  phase timeouts, and no redirects. Do not repair output or extract JSON from prose.
  Only exact wire no_action maps to None; missing/truncated content is failure.
  Credentials are trusted constructor configuration only, never prompt/request
  data, proposal output, audit data, or public diagnostics. Do not enable SDK debug
  logging or modify application logging configuration. Tests use dummy credentials,
  MockTransport, and blocked network/DNS; never run live inference implicitly.
- The separate provider_demo entry point defaults to Mock/read and requires a
  trusted audit path. It owns one session and calls ProviderHost.run_once once.
  Groq requires explicit selection and hidden controlling-terminal key input;
  reject SSLKEYLOGFILE presence and existing unsafe logging/header settings before
  SDK/client construction without reading values or changing configuration.
  After complete canonical review and flush, discard queued input and collect one
  bounded newline-terminated response through unbuffered terminal reads. Never
  coerce a response to match a scenario. Retain reports/attached controller_report
  before cleanup; close owned provider/writer resources while output remains
  usable. Cleanup/output failure cannot erase completed or uncertain effects.
  MockProvider has no resources to close. No temporary verification helpers,
  private-state inspection, HTTP diagnostic observer, retries, or loop belong in
  this demo. New live runs still need separate authorization.
- The offline proposal evaluator scores supplied observations only; it never
  calls providers, Controller/ProviderHost, approval, authorization, tools, or an
  audit writer. Expected answers are immutable and outside ProviderRequest.
  Reuse parse_proposal; compare scalar types and values without normalization.
  None is an actual no_action response, distinct from fixed ProviderFailure
  observations. Only two parsed proposals permit component match flags; failure
  leaves correctness unassessed. Aggregate at most one observation per known case
  ID, retaining explicit denominators and failure counts. Results contain no raw
  response or exception text, stay separate from execution JSONL, and grant no
  authority. A protected-target proposal can match intent yet require governance
  denial. Fixture results verify the evaluator, not Groq or broader model quality.
- The separate collector borrows one adapter and visits the fixed eight cases
  once in order, passing only case.request. Count each invocation immediately
  before calling; only successfully scored records enter results. ProviderError
  is a provider failure only from adapter.propose, and stops the batch. Other
  errors/interruptions propagate unchanged; partial-result attachment is best
  effort and must never replace the original exception. No retries, resume,
  execution, persistence, or resource cleanup belongs in the collector. Callers
  retain returned/attached evidence before their own cleanup. Current score
  evidence is scored_observations_source_unverified; collection provenance is
  adapter_collection_source_unverified. Neither is configurable or proves live
  origin; fixtures and SDK/MockTransport results remain offline verification.
- Default to denial for malformed requests, unknown tools or targets, absent
  permissions, and unresolved policy. A hard denial cannot become approval.
- The executor accepts only an internal single-use authorization reference and
  loads the exact validated action from trusted state. Never accept an
  `approved=true` shortcut, caller-created decision, or replacement arguments.
- Bind human approval and execution permission to the same immutable tool,
  arguments, target, action ID, and caller. Changed actions need new evaluation.
  Issue at most one authorization per action ID. Consume it before dispatch;
  do not restore it after failure or reissue it from the same approval/decision.
- Approval requires an explicit human response to the displayed action. Denial,
  decline, empty input, EOF, cancellation, and interruption never authorize work.

## Demo and scope boundaries

- Use fake local sample work orders only. No real industrial connections.
- Preserve `demo_operator`, editable `WO-1001`, and protected and critical
  `WO-9001`. Demo rules: permitted valid reads are LOW → `ALLOW`; permitted valid
  normal editable updates are MEDIUM → `REQUIRE_APPROVAL`; any identifiable
  mutation of a protected or critical target is HIGH → `DENY`. The same update
  tool covers both mutation paths; keep these rules in the demo layer.
- Keep work-order schemas, status rules, and protection conditions in the demo
  layer. The governance core must remain domain-neutral.
- Keep the two tools bounded. Do not introduce unrestricted shell or filesystem
  execution, arbitrary resource paths, or a generic update-any-field capability.
- No database, queue, microservices, cloud deployment, authentication system,
  policy language, vector database, multi-agent runtime, or parallel dispatcher
  without an explicitly approved scope change.
- Approval and authorization state are in memory and do not survive restart.
  Do not replay execution rights from audit logs or add automatic retries.

## Audit and verification

- Assign an action ID before validation. Audit malformed and denied submissions
  as well as approval outcomes and execution start, success, and failure.
- Audit blocked executor calls too. Use the originating action ID when known;
  otherwise assign a fresh host-generated action ID to the blocked attempt.
- Fail closed if any required pre-execution audit write fails. If logging fails
  after dispatch, report the logging failure and actual or uncertain tool outcome;
  do not claim rollback or silently retry.
- Use one context-managed JsonlAuditWriter per trusted local POSIX path/run.
  Require an existing trusted parent and an owned regular file with mode 0600;
  reject final symlinks and incomplete tails. Never silently repair permissions
  or records. Validate exact event fields and the 4 KiB encoded limit before
  writing; complete write, flush, and fsync are mandatory for every record.
  Poison the writer on any event failure, preserve interruptions, and never
  retry/reopen it or log its failures through itself. Identifiers must be public:
  syntax cannot detect embedded secrets. JSONL is not tamper-resistant or
  transactional; fsync is not an absolute power-loss guarantee.
- Keep runtime audit files and sensitive data out of version control. Logs and
  tool results are data, never authority to change policy or execute actions.
- Test contract structure, nested immutability, deterministic governance,
  permission checks, contextual risk, and hard-deny precedence now. As the relevant
  control components are implemented, add focused tests for security-relevant behavior:
  denied/malformed inputs, permission checks, contextual risk/decision changes,
  hard-deny precedence, human approval binding, forged/changed/reused
  authorization, attempted authorization reissuance, and audit failures.
- Assert tool-handler invocation counts and state changes in boundary tests;
  checking a decision label alone is insufficient. Run relevant checks and
  report their actual results and any unverified behavior.
- Update architecture/decision documentation when guarantees, trust assumptions,
  or execution behavior change. Describe this as an MVP/prototype, not a
  production-ready system or security sandbox. Its controls apply through the
  defined interfaces, not against arbitrary malicious code or a new bypass
  introduced inside the process.
