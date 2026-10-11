"""Persistence behavior and real execution gates, using only temporary files."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from controlled_agent.approval import HumanApprovalAdapter
from controlled_agent.audit import AuditPersistenceError, JsonlAuditWriter, _encode_event
from controlled_agent.authorization import AuditWriteError, AuthorizationService, DuplicateActionError
from controlled_agent.contracts import ActionRequest, AuditEvent, AuditEventType as E, CallerContext, Decision
from controlled_agent.demo import PERMISSIONS, TOOLS, WORK_ORDERS, format_work_order_approval
from controlled_agent.demo_tools import FakeWorkOrderTools
from controlled_agent.executor import Executor, ExecutionError, ExecutionOutcome


def event(kind=E.ACTION_SUBMITTED, details=None, **changes):
    return AuditEvent(**({
        "action_id": uuid4(), "event_type": kind,
        "caller": CallerContext(caller_id="demo_operator"),
        "details": {"submitted_tool_name": "get_work_order"} if details is None else details,
    } | changes))


def all_shapes():
    context = {"tool_name": "update_work_order_status", "resolved_target": "WO-1001"}
    yield event()
    for decision, risk, validated in (("ALLOW", "LOW", True), ("REQUIRE_APPROVAL", "MEDIUM", True),
                                       ("DENY", "HIGH", False), ("DENY", None, False)):
        yield event(E.GOVERNANCE_DECISION, context | {
            "decision": decision, "risk": risk, "action_validated": validated,
            "reason_code": "POLICY_RESULT",
        })
    yield event(E.APPROVAL_REQUESTED, context)
    for outcome, reason in (("approved", "HUMAN_APPROVED"), ("declined", "HUMAN_DECLINED"),
                            ("cancelled", "NO_RESPONSE"), ("cancelled", "INVALID_RESPONSE"),
                            ("cancelled", "END_OF_INPUT"), ("cancelled", "INTERRUPTED"),
                            ("error", "REVIEW_FAILED")):
        yield event(E.APPROVAL_RESULT, context | {"outcome": outcome, "reason_code": reason})
    yield event(E.AUTHORIZATION_ISSUED, {"authorization_id": str(uuid4()),
                                       "tool_name": "get_work_order", "target": "WO-1001"})
    for reason in ("UNKNOWN_AUTHORIZATION", "AUTHORIZATION_CONSUMED"):
        yield event(E.EXECUTION_BLOCKED, {"reason_code": reason}, caller=None)
    for kind, outcome, reason in (
        (E.EXECUTION_BLOCKED, "no_dispatch", "HANDLER_UNAVAILABLE"),
        (E.EXECUTION_STARTED, "no_dispatch", "EXECUTION_START"),
        (E.EXECUTION_SUCCEEDED, "succeeded", "EXECUTED"),
        (E.EXECUTION_FAILED, "succeeded", "RESULT_INVALID"),
        (E.EXECUTION_FAILED, "possibly_partial", "HANDLER_FAILED"),
        (E.EXECUTION_FAILED, "possibly_partial", "HANDLER_INTERRUPTED"),
        (E.EXECUTION_FAILED, "rejected_without_mutation", "INVALID_ACTION"),
        (E.EXECUTION_FAILED, "rejected_without_mutation", "PROTECTED_TARGET"),
        (E.EXECUTION_FAILED, "rejected_without_mutation", "STALE_BASELINE"),
    ):
        yield event(kind, {"tool_name": "get_work_order", "target": "WO-1001",
                           "outcome": outcome, "reason_code": reason})


@unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "POSIX file policy")
class AuditWriterTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "audit.jsonl"

    def writer(self):
        writer = JsonlAuditWriter(self.path)
        self.addCleanup(writer.close)
        return writer

    def spy(self, writer):
        real = writer._file
        self.addCleanup(real.close)
        spy = Mock(wraps=real)
        writer._file = spy
        return real, spy

    def test_all_current_shapes_round_trip_in_append_order(self):
        events = list(all_shapes())
        with self.writer() as writer, patch("controlled_agent.audit.os.fsync", wraps=os.fsync) as sync:
            for item in events:
                self.assertIsNone(writer(item))
            self.assertEqual(sync.call_count, len(events))
        data = self.path.read_bytes()
        self.assertEqual(data.count(b"\n"), len(events))
        self.assertTrue(data.endswith(b"\n"))
        records = [json.loads(line) for line in data.splitlines()]
        self.assertEqual({r["event_type"] for r in records}, {e.value for e in E})
        for item, record in zip(events, records):
            self.assertEqual(set(record), {"action_id", "timestamp", "event_type", "caller", "details"})
            self.assertEqual(record["action_id"], str(item.action_id))
            self.assertEqual(record["details"], dict(item.details))
            self.assertEqual(record["caller"], item.caller.caller_id if item.caller else None)
            self.assertEqual(record["timestamp"], item.timestamp.astimezone(UTC).isoformat())
            self.assertLessEqual(len(_encode_event(item)), 4096)

    def test_timestamp_is_normalized_to_utc_and_json_is_compact(self):
        instant = datetime(2026, 10, 11, 12, 0, 1, 123456, timezone(timedelta(hours=3)))
        item = event(timestamp=instant)
        with self.writer() as writer:
            writer(item)
        raw = self.path.read_bytes()
        self.assertNotIn(b": ", raw)
        self.assertNotIn(b", ", raw)
        self.assertEqual(raw.count(b"\n"), 1)
        self.assertEqual(json.loads(raw)["timestamp"], "2026-10-11T09:00:01.123456+00:00")

    def test_mappingproxy_is_serialized_explicitly_and_null_identifiers_preserved(self):
        item = event(details=MappingProxyType({"submitted_tool_name": None}), caller=None)
        self.assertIsInstance(item.details, MappingProxyType)
        with self.writer() as writer:
            writer(item)
        self.assertEqual(json.loads(self.path.read_bytes())["details"], {"submitted_tool_name": None})

    def test_every_shape_rejects_extra_or_missing_fields(self):
        for item in all_shapes():
            for details in (dict(item.details) | {"arguments": {"secret": "PRIVATE"}}, {},
                            {k: v for k, v in item.details.items() if k != next(iter(item.details))}):
                with self.subTest(kind=item.event_type, fields=set(details)):
                    with self.assertRaises(AuditPersistenceError):
                        _encode_event(replace(item, details=details))

    def test_sensitive_fields_are_rejected_not_dropped(self):
        for name in ("arguments", "result", "review", "response", "reference", "exception", "approved"):
            with self.subTest(name=name):
                writer = self.writer()
                _, spy = self.spy(writer)
                with self.assertRaises(AuditPersistenceError):
                    writer(event(details={"submitted_tool_name": "get_work_order", name: "SECRET"}))
                spy.write.assert_not_called()
                self.assertEqual(self.path.read_bytes(), b"")
                writer.close()

    def test_invalid_identifier_bounds_and_types_rejected(self):
        for bad in ("", "x" * 129, "a\nb", "a b", "é", "\x00", 7, True, {}, (), object()):
            with self.subTest(value_type=type(bad)):
                item = event()
                # Deliberately corrupt a frozen contract to exercise sink defenses.
                object.__setattr__(item, "details", {"submitted_tool_name": bad})
                with self.assertRaises(AuditPersistenceError):
                    _encode_event(item)
        _encode_event(event(details={"submitted_tool_name": "a" * 128}))
        for caller in ("x" * 129, "contains space", "a\nb"):
            with self.assertRaises(AuditPersistenceError):
                _encode_event(event(caller=CallerContext(caller_id=caller)))

    def test_cycles_nonfinite_numbers_and_nested_tuples_are_rejected(self):
        cycle = []
        cycle.append(cycle)
        mapping_cycle = {}
        mapping_cycle["loop"] = mapping_cycle
        for bad in (cycle, mapping_cycle, ("plain",), MappingProxyType({"x": "y"}),
                    float("nan"), float("inf"), -float("inf"), 0.1, b"bytes"):
            item = event()
            object.__setattr__(item, "details", {"submitted_tool_name": bad})
            writer = self.writer()
            _, spy = self.spy(writer)
            with self.assertRaises(AuditPersistenceError):
                writer(item)
            spy.write.assert_not_called()
            self.assertEqual(self.path.read_bytes(), b"")
            writer.close()

    def test_malformed_envelope_is_rejected(self):
        for name, bad in (("action_id", "claimed-id"), ("caller", {}), ("timestamp", datetime.now()),
                          ("timestamp", "now"), ("event_type", "action_submitted"), ("details", [])):
            item = event()
            object.__setattr__(item, name, bad)
            with self.subTest(field=name), self.assertRaises(AuditPersistenceError):
                _encode_event(item)
        with self.assertRaises(AuditPersistenceError):
            _encode_event({})

    def test_invalid_codes_flags_uuid_and_outcome_combinations_rejected(self):
        for item in all_shapes():
            for key, bad in (("reason_code", "private error text"), ("authorization_id", "not-uuid"),
                             ("action_validated", 1), ("risk", "UNKNOWN"), ("decision", "APPROVE"),
                             ("outcome", "approved")):
                if key in item.details:
                    # 'approved' is valid only for the affirmative approval event.
                    if key == "outcome" and item.details[key] == "approved":
                        bad = "declined"
                    with self.subTest(kind=item.event_type, field=key), self.assertRaises(AuditPersistenceError):
                        _encode_event(replace(item, details=dict(item.details) | {key: bad}))

    def test_record_byte_limit_includes_newline(self):
        item = event()
        size = len(_encode_event(item))
        with patch("controlled_agent.audit.MAX_RECORD_BYTES", size):
            self.assertEqual(len(_encode_event(item)), size)
        writer = self.writer()
        _, spy = self.spy(writer)
        with patch("controlled_agent.audit.MAX_RECORD_BYTES", size - 1):
            with self.assertRaises(AuditPersistenceError):
                writer(item)
        spy.write.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b"")
        # Independent defense even if the encoder grows beyond current schemas.
        with patch("controlled_agent.audit.json.dumps", return_value="x" * 4096):
            with self.assertRaises(AuditPersistenceError):
                _encode_event(item)

    def test_serialization_failure_poisoned_before_io(self):
        writer = self.writer()
        _, spy = self.spy(writer)
        error = ValueError("private serialization diagnostic")
        with patch("controlled_agent.audit.json.dumps", side_effect=error):
            with self.assertRaises(ValueError) as caught:
                writer(event())
        self.assertIs(caught.exception, error)
        with self.assertRaises(AuditPersistenceError):
            writer(event())
        spy.write.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b"")

    def test_write_then_flush_then_fsync_before_success(self):
        writer = self.writer()
        real, spy = self.spy(writer)
        calls = []
        def write(line):
            calls.append("write")
            self.assertEqual(line, _encode_event(item))
            return real.write(line)
        def flush():
            calls.append("flush")
            real.flush()
        sync = os.fsync
        def fsync(fd):
            calls.append("fsync")
            self.assertEqual(fd, real.fileno())
            sync(fd)
        spy.write.side_effect = write
        spy.flush.side_effect = flush
        item = event()
        with patch("controlled_agent.audit.os.fsync", side_effect=fsync):
            writer(item)
        self.assertEqual(calls, ["write", "flush", "fsync"])
        self.assertEqual(self.path.read_bytes(), _encode_event(item))

    def test_short_or_invalid_write_count_poisoned_without_retry_flush_or_sync(self):
        for count in (0, 1, None, True, 99999):
            writer = self.writer()
            _, spy = self.spy(writer)
            spy.write.return_value = count
            with patch("controlled_agent.audit.os.fsync") as sync:
                with self.assertRaises(AuditPersistenceError):
                    writer(event())
                with self.assertRaises(AuditPersistenceError):
                    writer(event())
                sync.assert_not_called()
            spy.write.assert_called_once()
            spy.flush.assert_not_called()
            writer.close()

    def test_write_flush_sync_errors_and_interruptions_permanently_poison(self):
        for stage in ("write", "flush", "fsync"):
            for error in (OSError("private"), KeyboardInterrupt(), SystemExit(3)):
                with self.subTest(stage=stage, error=type(error)):
                    writer = self.writer()
                    _, spy = self.spy(writer)
                    sync = Mock(wraps=os.fsync)
                    (sync if stage == "fsync" else getattr(spy, stage)).side_effect = error
                    with patch("controlled_agent.audit.os.fsync", sync):
                        with self.assertRaises(type(error)) as caught:
                            writer(event())
                        self.assertIs(caught.exception, error)
                        before = (spy.write.call_count, spy.flush.call_count, sync.call_count)
                        with self.assertRaises(AuditPersistenceError):
                            writer(event())
                        self.assertEqual(before, (spy.write.call_count, spy.flush.call_count, sync.call_count))
                    writer.close()

    def test_partial_bytes_remain_without_repair_or_retry(self):
        writer = self.writer()
        real, spy = self.spy(writer)
        spy.write.side_effect = lambda line: real.write(line[:15])
        with self.assertRaises(AuditPersistenceError):
            writer(event())
        writer.close()
        partial = self.path.read_bytes()
        self.assertEqual(len(partial), 15)
        with self.assertRaises(AuditPersistenceError):
            JsonlAuditWriter(self.path)
        self.assertEqual(self.path.read_bytes(), partial)

    def test_close_is_idempotent_and_closed_writer_rejects_write_and_enter(self):
        writer = self.writer()
        _, spy = self.spy(writer)
        writer.close()
        writer.close()
        spy.close.assert_called_once()
        with self.assertRaises(AuditPersistenceError):
            writer(event())
        with self.assertRaises(AuditPersistenceError):
            writer.__enter__()
        spy.write.assert_not_called()

    def test_close_failure_propagates_and_does_not_retry(self):
        for error in (OSError("private close error"), KeyboardInterrupt()):
            writer = self.writer()
            _, spy = self.spy(writer)
            spy.close.side_effect = error
            with self.assertRaises(type(error)) as caught:
                with writer:
                    writer(event())
            self.assertIs(caught.exception, error)
            writer.close()
            spy.close.assert_called_once()
            with self.assertRaises(AuditPersistenceError):
                writer(event())

    def test_cleanup_never_masks_original_failure_or_interruption(self):
        for original in (OSError("private write error"), KeyboardInterrupt(), SystemExit(9)):
            writer = self.writer()
            _, spy = self.spy(writer)
            spy.write.side_effect = original
            spy.close.side_effect = RuntimeError("private cleanup error")
            with self.assertRaises(type(original)) as caught:
                with writer:
                    writer(event())
            self.assertIs(caught.exception, original)
            self.assertEqual(original.__notes__, ["Audit writer cleanup also failed"])
            spy.write.assert_called_once()
            spy.close.assert_called_once()

    def test_append_reopen_preserves_records_and_never_truncates(self):
        first, second = event(), event()
        with self.writer() as writer:
            writer(first)
        with self.writer() as writer:
            writer(second)
        self.assertEqual(self.path.read_bytes(), _encode_event(first) + _encode_event(second))
        self.assertEqual(self.path.stat().st_mode & 0o7777, 0o600)

    def test_existing_incomplete_tail_rejected_unchanged(self):
        for content in (b"{", b'{"partial":true}', b"valid\npartial", b"\r"):
            self.path.write_bytes(content)
            self.path.chmod(0o600)
            with self.assertRaises(AuditPersistenceError):
                JsonlAuditWriter(self.path)
            self.assertEqual(self.path.read_bytes(), content)

    def test_newline_tail_is_not_historical_record_validation(self):
        self.path.write_bytes(b"unverified historical content\n")
        self.path.chmod(0o600)
        item = event()
        with self.writer() as writer:
            writer(item)
        self.assertEqual(self.path.read_bytes(), b"unverified historical content\n" + _encode_event(item))

    def test_unsuitable_existing_permissions_rejected_without_chmod_or_write(self):
        for mode in (0o644, 0o660, 0o666, 0o400, 0o700, 0o1600):
            self.path.touch()
            self.path.chmod(mode)
            with self.subTest(mode=oct(mode)), self.assertRaises((AuditPersistenceError, PermissionError)):
                JsonlAuditWriter(self.path)
            self.assertEqual(self.path.stat().st_mode & 0o7777, mode)
            self.assertEqual(self.path.stat().st_size, 0)
            self.path.chmod(0o600)

    def test_descriptor_owner_checked_and_fd_closed_on_rejection(self):
        real_fstat = os.fstat
        def foreign(fd):
            info = real_fstat(fd)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=os.geteuid() + 1, st_size=info.st_size)
        with patch("controlled_agent.audit.os.fstat", side_effect=foreign), \
             patch("controlled_agent.audit.os.close", wraps=os.close) as close:
            with self.assertRaises(AuditPersistenceError):
                JsonlAuditWriter(self.path)
        close.assert_called_once()

    def test_open_uses_append_nofollow_nonblocking_and_private_creation_mode(self):
        with patch("controlled_agent.audit.os.open", wraps=os.open) as opening:
            writer = self.writer()
        path, flags, mode = opening.call_args.args
        self.assertEqual(path, self.path)
        self.assertEqual(mode, 0o600)
        for flag in (os.O_APPEND, os.O_CREAT, os.O_NOFOLLOW, os.O_NONBLOCK):
            self.assertTrue(flags & flag)
        self.assertFalse(flags & os.O_TRUNC)
        self.assertFalse(os.get_inheritable(writer._file.fileno()))

    def test_unsupported_platform_fails_without_opening(self):
        with patch("controlled_agent.audit.os.name", "nt"), \
             patch("controlled_agent.audit.os.open") as opening:
            with self.assertRaises(AuditPersistenceError):
                JsonlAuditWriter(self.path)
            opening.assert_not_called()

    def test_descriptor_setup_failure_closes_fd_and_preserves_primary_interruption(self):
        error = KeyboardInterrupt()
        real_close = os.close
        def close_then_fail(fd):
            real_close(fd)
            raise OSError("PRIVATE_CLEANUP")
        with patch("controlled_agent.audit.os.fdopen", side_effect=error), \
             patch("controlled_agent.audit.os.close", side_effect=close_then_fail) as close:
            with self.assertRaises(KeyboardInterrupt) as caught:
                JsonlAuditWriter(self.path)
        self.assertIs(caught.exception, error)
        self.assertEqual(error.__notes__, ["Audit descriptor cleanup also failed"])
        close.assert_called_once()

    def test_final_symlink_fifo_and_directory_are_rejected(self):
        target = self.path.with_name("real.jsonl")
        target.write_bytes(b"sentinel\n")
        target.chmod(0o600)
        self.path.symlink_to(target)
        with self.assertRaises(OSError):
            JsonlAuditWriter(self.path)
        self.assertEqual(target.read_bytes(), b"sentinel\n")
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        with self.assertRaises(AuditPersistenceError):
            JsonlAuditWriter(self.path)
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises((AuditPersistenceError, OSError)):
            JsonlAuditWriter(self.path)

    def test_missing_parent_open_and_tail_read_failures_are_not_retried(self):
        with self.assertRaises(FileNotFoundError):
            JsonlAuditWriter(self.path / "missing" / "audit.jsonl")
        self.assertFalse(self.path.exists())
        with patch("controlled_agent.audit.os.open", side_effect=OSError("open failure")) as opening:
            with self.assertRaises(OSError):
                JsonlAuditWriter(self.path)
            opening.assert_called_once()
        self.path.write_bytes(b"existing\n")
        self.path.chmod(0o600)
        with patch("controlled_agent.audit.os.pread", side_effect=KeyboardInterrupt) as reading, \
             patch("controlled_agent.audit.os.close", wraps=os.close) as close:
            with self.assertRaises(KeyboardInterrupt):
                JsonlAuditWriter(self.path)
            reading.assert_called_once()
            close.assert_called_once()
        self.assertEqual(self.path.read_bytes(), b"existing\n")


@unittest.skipUnless(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "POSIX file policy")
class AuditIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "audit.jsonl"
        self.writer = JsonlAuditWriter(self.path)
        self.addCleanup(self.writer.close)
        self.response = Mock(return_value="approve")
        self.views = []
        self.service = self.service_with()
        self.tools = FakeWorkOrderTools(authorization=self.service)
        self.handlers = {key: Mock(wraps=value) for key, value in self.tools.handlers.items()}
        self.executor = Executor(authorization=self.service, handlers=self.handlers)

    def service_with(self, **changes):
        return AuthorizationService(**({
            "tools": TOOLS, "permissions": PERMISSIONS, "resources": WORK_ORDERS,
            "audit_write": self.writer, "approval_formatter": format_work_order_approval,
            "human_approval": HumanApprovalAdapter(display=self.views.append, read_response=self.response),
        } | changes))

    def request(self, tool="get_work_order", target="WO-1001", **changes):
        arguments = {"work_order_id": target}
        if tool == "update_work_order_status":
            arguments["new_status"] = "closed"
        return ActionRequest(**({"action_id": uuid4(), "caller": CallerContext(caller_id="demo_operator"),
                                "tool_name": tool, "arguments": arguments} | changes))

    def records(self):
        return [json.loads(line) for line in self.path.read_bytes().splitlines()]

    def no_effects(self):
        for handler in self.handlers.values():
            handler.assert_not_called()
        self.assertEqual(self.tools._statuses, {"WO-1001": "open", "WO-9001": "open"})

    def fail_sync_on(self, event_type, error=None):
        # Real bytes are written; only the durability acknowledgement fails.
        # Even a complete visible log line must never be treated as authority.
        sync = os.fsync
        def fail(fd):
            if self.records()[-1]["event_type"] == event_type.value:
                raise error if error is not None else OSError("PRIVATE_SYNC_ERROR")
            sync(fd)
        return patch("controlled_agent.audit.os.fsync", side_effect=fail)

    def test_real_allow_and_approved_update_counts_state_identity_and_order(self):
        read = self.request()
        _, reference = self.service.issue(read)
        self.executor.execute(reference)
        update = self.request("update_work_order_status")
        _, reference = self.service.issue(update)
        self.executor.execute(reference)
        self.handlers["get_work_order"].assert_called_once()
        self.handlers["update_work_order_status"].assert_called_once()
        self.assertIs(self.handlers["update_work_order_status"].call_args.args[0], self.views[0].action)
        self.assertEqual(self.tools._statuses, {"WO-1001": "closed", "WO-9001": "open"})
        kinds = [r["event_type"] for r in self.records() if r["action_id"] == str(update.action_id)]
        self.assertEqual(kinds, ["action_submitted", "governance_decision", "approval_requested",
                                 "approval_result", "authorization_issued", "execution_started", "execution_succeeded"])
        with self.assertRaises(ExecutionError):
            self.executor.execute(reference)
        self.handlers["update_work_order_status"].assert_called_once()
        self.assertEqual(self.records()[-1]["details"], {"reason_code": "AUTHORIZATION_CONSUMED"})

    def test_protected_deny_decline_cancel_and_malformed_never_dispatch(self):
        result, reference = self.service.issue(self.request("update_work_order_status", "WO-9001"))
        self.assertIs(result.decision, Decision.DENY)
        self.assertIsNone(reference)
        self.response.assert_not_called()
        decision = self.records()[-1]["details"]
        self.assertEqual(decision["resolved_target"], "WO-9001")
        self.assertFalse(decision["action_validated"])
        for response in ("decline", None, "SECRET_RESPONSE"):
            self.response.return_value = response
            _, reference = self.service.issue(self.request("update_work_order_status"))
            self.assertIsNone(reference)
        _, reference = self.service.issue(self.request(arguments={"work_order_id": "SECRET_UNKNOWN_TARGET"}))
        self.assertIsNone(reference)
        with self.assertRaises(ExecutionError):
            self.executor.execute(reference)
        self.no_effects()
        data = self.path.read_text()
        self.assertNotIn("SECRET_RESPONSE", data)
        self.assertNotIn("SECRET_UNKNOWN_TARGET", data)
        self.assertNotIn("authorization_issued", data)

    def test_issuance_sync_failure_all_gates_prevents_authority_and_reserves_id(self):
        for gate in (E.ACTION_SUBMITTED, E.GOVERNANCE_DECISION, E.APPROVAL_REQUESTED,
                     E.APPROVAL_RESULT, E.AUTHORIZATION_ISSUED):
            with self.subTest(gate=gate):
                # Each subcase is a separate run, with its own failed writer/service.
                with JsonlAuditWriter(self.path) as writer:
                    service = self.service_with(audit_write=writer)
                    request = self.request("update_work_order_status")
                    with self.fail_sync_on(gate), self.assertRaises(AuditWriteError):
                        service.issue(request)
                    self.assertEqual(service._records, {})
                    self.assertIn(request.action_id, service._seen)
                    with self.assertRaises(AuditWriteError):
                        service.issue(request)
                    executor = Executor(authorization=service, handlers=self.handlers)
                    with self.assertRaises(ExecutionError):
                        executor.execute(None)
                    self.no_effects()

    def test_execution_start_sync_failure_consumes_before_zero_handler_calls(self):
        request = self.request("update_work_order_status")
        _, reference = self.service.issue(request)
        with self.fail_sync_on(E.EXECUTION_STARTED), self.assertRaises(ExecutionError) as caught:
            self.executor.execute(reference)
        self.assertIs(caught.exception.report.outcome, ExecutionOutcome.NO_DISPATCH)
        self.assertTrue(caught.exception.report.audit_failed)
        self.assertTrue(self.service._records[reference].consumed)
        with self.assertRaises(ExecutionError):
            self.executor.execute(reference)
        self.no_effects()

    def test_write_and_flush_failures_at_issuance_start_and_completion_gate_real_effects(self):
        for stage in ("write", "flush"):
            for gate in (E.AUTHORIZATION_ISSUED, E.EXECUTION_STARTED, E.EXECUTION_SUCCEEDED):
                with self.subTest(stage=stage, gate=gate):
                    path = self.path.with_name(f"{stage}-{gate.value}.jsonl")
                    with JsonlAuditWriter(path) as writer:
                        service = self.service_with(audit_write=writer)
                        fixture = FakeWorkOrderTools(authorization=service)
                        handler = Mock(wraps=fixture.handlers["update_work_order_status"])
                        executor = Executor(authorization=service, handlers={"update_work_order_status": handler})
                        real = writer._file
                        spy = Mock(wraps=real)
                        writer._file = spy
                        current = None
                        def write(line):
                            nonlocal current
                            current = json.loads(line)["event_type"]
                            if stage == "write" and current == gate.value:
                                raise OSError("PRIVATE_WRITE_FAILURE")
                            return real.write(line)
                        def flush():
                            if stage == "flush" and current == gate.value:
                                raise OSError("PRIVATE_FLUSH_FAILURE")
                            real.flush()
                        spy.write.side_effect = write
                        spy.flush.side_effect = flush
                        request = self.request("update_work_order_status")
                        if gate is E.AUTHORIZATION_ISSUED:
                            with self.assertRaises(AuditWriteError):
                                service.issue(request)
                            self.assertFalse(service._records)
                            self.assertIn(request.action_id, service._seen)
                        else:
                            _, reference = service.issue(request)
                            with self.assertRaises(ExecutionError) as caught:
                                executor.execute(reference)
                            expected = (ExecutionOutcome.SUCCEEDED if gate is E.EXECUTION_SUCCEEDED
                                        else ExecutionOutcome.NO_DISPATCH)
                            self.assertIs(caught.exception.report.outcome, expected)
                            self.assertTrue(caught.exception.report.audit_failed)
                            self.assertTrue(service._records[reference].consumed)
                            with self.assertRaises(ExecutionError):
                                executor.execute(reference)
                        completed = gate is E.EXECUTION_SUCCEEDED
                        self.assertEqual(handler.call_count, int(completed))
                        self.assertEqual(fixture._statuses["WO-1001"], "closed" if completed else "open")
                        self.assertEqual(fixture._statuses["WO-9001"], "open")

    def test_completion_sync_failure_preserves_actual_mutation_and_consumed_reference(self):
        _, reference = self.service.issue(self.request("update_work_order_status"))
        with self.fail_sync_on(E.EXECUTION_SUCCEEDED), self.assertRaises(ExecutionError) as caught:
            self.executor.execute(reference)
        self.assertIs(caught.exception.report.outcome, ExecutionOutcome.SUCCEEDED)
        self.assertTrue(caught.exception.report.audit_failed)
        self.assertFalse(caught.exception.report.completion_audited)
        self.assertEqual(self.tools._statuses["WO-1001"], "closed")
        with self.assertRaises(ExecutionError):
            self.executor.execute(reference)
        self.handlers["update_work_order_status"].assert_called_once()
        self.assertTrue(self.service._records[reference].consumed)

    def test_partial_handler_failure_and_failed_failure_audit_retain_both_diagnostics(self):
        def handler(action):
            self.tools._statuses[action.target] = "closed"
            raise ValueError("PRIVATE_HANDLER_ERROR")
        counted = Mock(side_effect=handler)
        executor = Executor(authorization=self.service, handlers={"update_work_order_status": counted})
        _, reference = self.service.issue(self.request("update_work_order_status"))
        with self.fail_sync_on(E.EXECUTION_FAILED), self.assertRaises(ExecutionError) as caught:
            executor.execute(reference)
        self.assertIs(caught.exception.report.outcome, ExecutionOutcome.UNCERTAIN)
        self.assertTrue(caught.exception.report.audit_failed)
        self.assertIsInstance(caught.exception.handler_error, ValueError)
        self.assertIsInstance(caught.exception.audit_error, AuditWriteError)
        self.assertEqual(self.tools._statuses["WO-1001"], "closed")
        counted.assert_called_once()
        with self.assertRaises(ExecutionError):
            executor.execute(reference)
        counted.assert_called_once()
        self.assertNotIn("PRIVATE_", self.path.read_text())

    def test_sync_interruption_preserves_type_and_no_dispatch_before_start(self):
        _, reference = self.service.issue(self.request("update_work_order_status"))
        error = KeyboardInterrupt()
        with self.fail_sync_on(E.EXECUTION_STARTED, error), self.assertRaises(KeyboardInterrupt) as caught:
            self.executor.execute(reference)
        self.assertIs(caught.exception, error)
        self.assertIs(error.execution_report.outcome, ExecutionOutcome.NO_DISPATCH)
        self.assertTrue(error.execution_report.audit_failed)
        self.assertTrue(self.service._records[reference].consumed)
        self.no_effects()

    def test_both_blocked_shapes_and_handler_failure_are_persisted(self):
        with self.assertRaises(ExecutionError):
            self.executor.execute(object())
        self.assertEqual(self.records()[-1]["details"], {"reason_code": "UNKNOWN_AUTHORIZATION"})
        _, reference = self.service.issue(self.request())
        empty = Executor(authorization=self.service, handlers={})
        with self.assertRaises(ExecutionError):
            empty.execute(reference)
        self.assertEqual(self.records()[-1]["details"], {
            "tool_name": "get_work_order", "target": "WO-1001",
            "outcome": "no_dispatch", "reason_code": "HANDLER_UNAVAILABLE",
        })
        self.no_effects()
        handler = Mock(side_effect=ValueError("SECRET_EXCEPTION"))
        failing = Executor(authorization=self.service, handlers={"get_work_order": handler})
        _, reference = self.service.issue(self.request())
        with self.assertRaises(ExecutionError) as caught:
            failing.execute(reference)
        self.assertTrue(caught.exception.report.completion_audited)
        handler.assert_called_once()
        self.assertNotIn("SECRET_EXCEPTION", self.path.read_text())

    def test_permissive_schema_never_persists_arguments_results_or_reference(self):
        rules = replace(TOOLS["get_work_order"], validate_arguments=lambda _: True)
        service = self.service_with(tools={"get_work_order": rules})
        handler = Mock(return_value={"sensitive": "SECRET_RESULT"})
        executor = Executor(authorization=service, handlers={"get_work_order": handler})
        _, reference = service.issue(self.request(arguments={"work_order_id": "WO-1001", "secret": "SECRET_ARGUMENT"}))
        executor.execute(reference)
        handler.assert_called_once()
        for secret in ("SECRET_RESULT", "SECRET_ARGUMENT", repr(reference)):
            self.assertNotIn(secret, self.path.read_text())
        # A persisted descriptive UUID cannot substitute for a private reference.
        identifier = next(r["details"]["authorization_id"] for r in self.records()
                          if r["event_type"] == "authorization_issued")
        with self.assertRaises(ExecutionError):
            executor.execute(identifier)
        handler.assert_called_once()
        with self.assertRaises(DuplicateActionError):
            service.issue(handler.call_args.args[0])

    def test_new_service_never_restores_rights_from_existing_audit_file(self):
        _, reference = self.service.issue(self.request())
        self.writer.close()
        with JsonlAuditWriter(self.path) as writer:
            other = self.service_with(audit_write=writer)
            executor = Executor(authorization=other, handlers=self.handlers)
            with self.assertRaises(ExecutionError):
                executor.execute(reference)
        self.no_effects()


if __name__ == "__main__":
    unittest.main()
