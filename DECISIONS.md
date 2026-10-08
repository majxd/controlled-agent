# Architectural Decisions

This records the agreed direction for Controlled Agent's first MVP. Milestone 2A contracts are approved and committed. Milestone 2B deterministic governance and demo configuration are implemented for review. Approval, authorization state, execution, audit storage, CLI, and model/provider integration remain deferred.

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

**Consequence:** Missing, invalid, mismatched, pending, denied, or already-consumed authorization cannot reach a tool. Authorization is single-use and does not survive restart. Governance issues at most one authorization per action ID; a consumed authorization cannot be reissued from the same decision or approval. Milestone 2A represents authorization data with a UUID, a complete immutable action, and usable/consumed state. Trusted issuance, consumption, reference resolution, and semantic normalization remain deferred; constructing this record grants no authority. This decision does not require cryptographic tokens or a separate service.

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

**Limitations:** An unavailable audit sink cannot guarantee a persisted record of its own failure; the CLI must surface that failure. A crash or logging failure after a side effect can leave an uncertain outcome. Local JSONL is not a transactional, tamper-proof, or exactly-once execution mechanism. Audit failures after execution must not be presented as proof that the side effect did not happen. [ARCHITECTURE.md](ARCHITECTURE.md) defines the conceptual events and data-minimization boundary; concrete serialization details remain for implementation.

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
