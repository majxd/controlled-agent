# Repository instructions

## Working agreement

- Act as an implementation partner. Follow the user's authorized milestone;
  do not silently expand product or architectural scope.
- Current status: Milestones 2A, 2B, 3A, 3B, 3C, 4A, and 4B are approved and committed.
  Milestone 4C end-to-end verification has passed and is approved; its closure is
  documentation only. The verified baseline has 228 passing Python 3.12.14 tests.
  See the Milestone 4C summary in ARCHITECTURE.md for automated pseudo-terminal
  evidence and limitations; it is not manual-human or production verification.
  No subsequent milestone is authorized.
  Do not begin model/provider integration or expand the CLI until authorized. Do not add
  dependencies or commit unless requested.
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
