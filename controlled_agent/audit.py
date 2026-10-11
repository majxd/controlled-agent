"""Strict local JSONL persistence for trusted lifecycle producers.

This POSIX-only, sequential sink retains identifiers and fixed outcome metadata,
not execution rights. The host owns the path, its parent directory, and the
public/non-sensitive meaning of permitted identifiers. Syntax cannot detect
secrets embedded in those strings. A failed append may have reached disk; never
retry it or infer tool effects/authority from the file.
"""

from collections.abc import Mapping
from datetime import UTC, datetime
import json
import os
from re import fullmatch
import stat
from uuid import UUID

from .contracts import AuditEvent, AuditEventType, CallerContext


MAX_RECORD_BYTES = 4096  # Includes the terminating LF.
MAX_IDENTIFIER_CHARS = 128


class AuditPersistenceError(RuntimeError):
    """Safe sink error; chained OS causes are private host diagnostics."""


def _identifier(value: object, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if (type(value) is not str or not 1 <= len(value) <= MAX_IDENTIFIER_CHARS
            or fullmatch(r"[A-Za-z0-9_.:-]+", value) is None):
        raise AuditPersistenceError("Invalid audit identifier")


def _reason(value: object) -> None:
    if (type(value) is not str or not 1 <= len(value) <= MAX_IDENTIFIER_CHARS
            or fullmatch(r"[A-Z][A-Z0-9_]*", value) is None):
        raise AuditPersistenceError("Invalid audit reason code")


def _uuid_text(value: object) -> None:
    if (type(value) is not str or len(value) != 36
            or fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value) is None):
        raise AuditPersistenceError("Invalid audit UUID")


def _details(event: AuditEvent) -> dict:
    # These are deliberately flat schemas. Mapping proxies are read explicitly;
    # nested mappings/tuples, cycles, numbers, and custom objects have no allowed
    # field here. Reject them rather than stringify, traverse, or silently drop.
    if not isinstance(event.details, Mapping):
        raise AuditPersistenceError("Audit details must be a mapping")
    data = dict(event.details)
    if any(type(key) is not str for key in data):
        raise AuditPersistenceError("Invalid audit field name")
    kind = event.event_type
    match kind:
        case AuditEventType.ACTION_SUBMITTED:
            fields = {"submitted_tool_name"}
        case AuditEventType.GOVERNANCE_DECISION:
            fields = {"decision", "risk", "reason_code", "tool_name",
                      "resolved_target", "action_validated"}
        case AuditEventType.APPROVAL_REQUESTED:
            fields = {"tool_name", "resolved_target"}
        case AuditEventType.APPROVAL_RESULT:
            fields = {"tool_name", "resolved_target", "outcome", "reason_code"}
        case AuditEventType.AUTHORIZATION_ISSUED:
            fields = {"authorization_id", "tool_name", "target"}
        case AuditEventType.EXECUTION_BLOCKED if set(data) == {"reason_code"}:
            fields = {"reason_code"}
        case (AuditEventType.EXECUTION_STARTED | AuditEventType.EXECUTION_SUCCEEDED
              | AuditEventType.EXECUTION_FAILED | AuditEventType.EXECUTION_BLOCKED):
            fields = {"tool_name", "target", "outcome", "reason_code"}
        case _:
            raise AuditPersistenceError("Unsupported audit event type")
    if set(data) != fields:
        raise AuditPersistenceError("Unexpected audit event fields")
    for key, value in data.items():
        if key in {"submitted_tool_name", "tool_name", "target", "resolved_target"}:
            _identifier(value, nullable=True)
        elif key == "authorization_id":
            _uuid_text(value)
        elif key == "reason_code":
            _reason(value)
        elif key == "action_validated":
            if type(value) is not bool:
                raise AuditPersistenceError("Invalid audit validation flag")
        elif key == "risk":
            if value is not None and (type(value) is not str or value not in {"LOW", "MEDIUM", "HIGH"}):
                raise AuditPersistenceError("Invalid audit risk")
        elif type(value) is not str:
            raise AuditPersistenceError("Invalid audit value type")
    if kind is AuditEventType.GOVERNANCE_DECISION:
        if data["decision"] not in {"ALLOW", "DENY", "REQUIRE_APPROVAL"}:
            raise AuditPersistenceError("Invalid audit decision")
    elif kind is AuditEventType.APPROVAL_RESULT:
        if (data["outcome"], data["reason_code"]) not in {
            ("approved", "HUMAN_APPROVED"), ("declined", "HUMAN_DECLINED"),
            ("cancelled", "NO_RESPONSE"), ("cancelled", "INVALID_RESPONSE"),
            ("cancelled", "END_OF_INPUT"), ("cancelled", "INTERRUPTED"),
            ("error", "REVIEW_FAILED"),
        }:
            raise AuditPersistenceError("Invalid audit approval outcome")
    elif kind is AuditEventType.EXECUTION_BLOCKED and fields == {"reason_code"}:
        if data["reason_code"] not in {"AUTHORIZATION_CONSUMED", "UNKNOWN_AUTHORIZATION"}:
            raise AuditPersistenceError("Invalid audit blocked reason")
    elif kind in {AuditEventType.EXECUTION_STARTED, AuditEventType.EXECUTION_SUCCEEDED,
                  AuditEventType.EXECUTION_FAILED, AuditEventType.EXECUTION_BLOCKED}:
        allowed = {
            AuditEventType.EXECUTION_STARTED: {("no_dispatch", "EXECUTION_START")},
            AuditEventType.EXECUTION_SUCCEEDED: {("succeeded", "EXECUTED")},
            AuditEventType.EXECUTION_BLOCKED: {("no_dispatch", "HANDLER_UNAVAILABLE")},
            AuditEventType.EXECUTION_FAILED: {
                ("possibly_partial", "HANDLER_FAILED"),
                ("possibly_partial", "HANDLER_INTERRUPTED"),
                ("rejected_without_mutation", "INVALID_ACTION"),
                ("rejected_without_mutation", "PROTECTED_TARGET"),
                ("rejected_without_mutation", "STALE_BASELINE"),
                ("succeeded", "RESULT_INVALID"),
            },
        }
        if (data["outcome"], data["reason_code"]) not in allowed[kind]:
            raise AuditPersistenceError("Invalid audit execution outcome")
    return data


def _encode_event(event: AuditEvent) -> bytes:
    if type(event) is not AuditEvent or type(event.event_type) is not AuditEventType:
        raise AuditPersistenceError("Expected a lifecycle AuditEvent")
    if type(event.action_id) is not UUID:
        raise AuditPersistenceError("Invalid audit action ID")
    if type(event.timestamp) is not datetime or event.timestamp.utcoffset() is None:
        raise AuditPersistenceError("Expected an aware audit timestamp")
    caller = None
    if event.caller is not None:
        if type(event.caller) is not CallerContext:
            raise AuditPersistenceError("Invalid audit caller")
        _identifier(event.caller.caller_id)
        caller = event.caller.caller_id
    record = {
        "action_id": str(event.action_id),
        "timestamp": event.timestamp.astimezone(UTC).isoformat(),
        "event_type": event.event_type.value,
        "caller": caller,
        "details": _details(event),
    }
    line = (json.dumps(record, ensure_ascii=False, allow_nan=False,
                       separators=(",", ":")) + "\n").encode("utf-8")
    if len(line) > MAX_RECORD_BYTES:
        raise AuditPersistenceError("Audit record exceeds byte limit")
    return line


class JsonlAuditWriter:
    """Context-managed callable: write one complete record, flush, then fsync.

    Requires POSIX O_NOFOLLOW, an existing trusted parent, and a regular file
    owned by the effective user with mode 0600. New files use 0600 subject to
    umask (a more restrictive resulting mode is rejected). One writer per file;
    no concurrent writers, repair, rotation, recovery, or rights reconstruction.
    Any event failure, including validation, permanently poisons this instance.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self._closed = False
        self._failed = False
        if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
            raise AuditPersistenceError("Secure audit opening requires POSIX O_NOFOLLOW")
        fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600):
                raise AuditPersistenceError("Audit file must be owned, regular, and mode 0600")
            if info.st_size and os.pread(fd, 1, info.st_size - 1) != b"\n":
                raise AuditPersistenceError("Audit file has an incomplete tail")
            # Unbuffered: close cannot flush buffered bytes after a failed append.
            self._file = os.fdopen(fd, "ab", buffering=0)
        except BaseException as error:
            try:
                os.close(fd)
            except BaseException:
                error.add_note("Audit descriptor cleanup also failed")
            raise

    def __call__(self, event: AuditEvent) -> None:
        if self._closed or self._failed:
            raise AuditPersistenceError("Audit writer is closed or failed")
        try:
            line = _encode_event(event)
            written = self._file.write(line)
            if type(written) is not int or written != len(line):
                raise AuditPersistenceError("Incomplete audit write")
            self._file.flush()
            os.fsync(self._file.fileno())
        except BaseException:
            self._failed = True
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._file.close()
        except BaseException:
            self._failed = True
            raise

    def __enter__(self):
        if self._closed or self._failed:
            raise AuditPersistenceError("Audit writer is closed or failed")
        return self

    def __exit__(self, exc_type, error, traceback):
        try:
            self.close()
        except BaseException:
            if error is None:
                raise
            error.add_note("Audit writer cleanup also failed")
        return False
