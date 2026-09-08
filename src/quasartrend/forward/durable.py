"""Crash-recoverable capture storage. No strategy or broker operations live here.

A newline terminates a committed frame. Only an unterminated tail can be
quarantined and removed; every terminated frame must validate exactly. A root
lock must be held throughout recovery and writing by the capture service.
"""
from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping


Fault = Callable[[str, Path], None]


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def no_fault(point: str, path: Path) -> None:
    pass


def sync_directory(path: Path) -> None:
    # Windows replace semantics and fsync(file) are used there. Directory fsync
    # is a POSIX durability facility, not a supported Windows file operation.
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def atomic_write(path: Path, text: str, fault: Fault = no_fault) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    fault("before_checkpoint_write", path)
    with temporary.open("wb") as stream:
        data = text.encode("utf-8")
        stream.write(data[:len(data) // 2])
        stream.flush()
        fault("during_checkpoint_write", path)
        stream.write(data[len(data) // 2:])
        stream.flush()
        os.fsync(stream.fileno())
    fault("before_checkpoint_replace", path)
    os.replace(temporary, path)
    sync_directory(path.parent)
    fault("after_checkpoint_replace", path)


class EvidenceLock:
    """Permanent lock inode; the OS releases ownership on process death.

    Never unlink a lock file: another process may already hold its inode. No
    PID timeout or stale-lock deletion is involved. Windows locks byte [0,1),
    including past EOF, so a rejected writer never writes even a marker byte.
    """
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / ".xm-forward.lock"
        self.stream = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.stream.close()
            raise PermissionError("forward evidence root already has a writer") from error

    def close(self) -> None:
        if self.stream.closed:
            return
        if os.name == "nt":
            import msvcrt
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        self.stream.close()


class JsonlJournal:
    """Canonical JSON frames with sequence, hash chain and exact logical IDs.

    An append error poisons the instance: an OS write may have succeeded even
    when its caller sees failure. Only re-opening and validating disk can decide.
    Legacy unframed evidence is never implicitly migrated.
    """
    def __init__(self, path: Path, *, schema: str, provenance: Mapping[str, Any],
                 time_field: str, id_field: str, fault: Fault = no_fault) -> None:
        self.path, self.schema, self.provenance = Path(path), schema, dict(provenance)
        self.time_field, self.id_field, self.fault = time_field, id_field, fault
        self._ids: dict[str, str] = {}
        self._rows: dict[str, dict[str, Any]] = {}
        self.last_time: int | None = None
        self.tip = "0" * 64
        self.digests: list[str] = []
        self.poisoned = False
        self._load()

    def _validate(self, row: Mapping[str, Any]) -> None:
        if not isinstance(row, dict) or row.get("schema_version") != self.schema or any(row.get(k) != v for k, v in self.provenance.items()):
            raise ValueError("journal schema/provenance mismatch")
        stamp, identity = row.get(self.time_field), row.get(self.id_field)
        if isinstance(stamp, bool) or not isinstance(stamp, int) or stamp < 0:
            raise ValueError("journal timestamp invalid")
        if not isinstance(identity, str) or not identity:
            raise ValueError("journal identity invalid")

    def _accept(self, row: dict[str, Any], encoded: str, digest: str) -> None:
        stamp, identity = row[self.time_field], row[self.id_field]
        if self.last_time is not None and stamp < self.last_time:
            raise ValueError("journal chronological regression")
        if identity in self._ids:
            raise ValueError("duplicate journal identity")
        self._ids[identity], self._rows[identity] = encoded, row
        self.last_time, self.tip = stamp, digest
        self.digests.append(digest)

    def _load(self) -> None:
        if not self.path.exists():
            return
        offset = 0
        with self.path.open("rb") as stream:
            for number, line in enumerate(stream, 1):
                if not line.endswith(b"\n"):
                    # Keep exact forensic bytes, including across a crash during
                    # recovery. Only this uncommitted suffix may be truncated.
                    quarantine = self.path.with_name(self.path.name + ".tail-" + sha256(line).hexdigest())
                    if quarantine.exists():
                        if quarantine.read_bytes() != line:
                            raise ValueError("quarantine evidence differs from uncommitted tail")
                    else:
                        temporary = quarantine.with_name(quarantine.name + ".tmp")
                        with temporary.open("wb") as tail:
                            tail.write(line[:len(line) // 2])
                            tail.flush()
                            self.fault("during_quarantine_write", self.path)
                            tail.write(line[len(line) // 2:])
                            tail.flush()
                            os.fsync(tail.fileno())
                        os.replace(temporary, quarantine)
                        sync_directory(quarantine.parent)
                    self.fault("after_quarantine_fsync", self.path)
                    with self.path.open("r+b") as repair:
                        repair.truncate(offset)
                        repair.flush()
                        os.fsync(repair.fileno())
                    self.fault("after_tail_truncate", self.path)
                    break
                try:
                    frame = json.loads(line)
                    if not isinstance(frame, dict) or set(frame) != {"format", "sequence", "previous", "payload", "digest"}:
                        raise ValueError("invalid frame fields")
                    unsigned = {key: value for key, value in frame.items() if key != "digest"}
                    digest = sha256(canonical(unsigned).encode()).hexdigest()
                    if (frame["format"] != "xm-capture-frame/v1" or type(frame["sequence"]) is not int
                            or frame["sequence"] != number or frame["previous"] != self.tip
                            or frame["digest"] != digest or line != (canonical(frame) + "\n").encode()):
                        raise ValueError("frame checksum/chain/framing mismatch")
                    row = frame["payload"]
                    self._validate(row)
                    self._accept(row, canonical(row), digest)
                except (ValueError, TypeError, KeyError, UnicodeError) as error:
                    raise ValueError(f"invalid committed journal row {number}: {error}") from error
                offset += len(line)

    def material(self, row: Mapping[str, Any]) -> dict[str, Any]:
        material = {"schema_version": self.schema, **self.provenance, **row}
        self._validate(material)
        return material

    def append(self, row: Mapping[str, Any]) -> bool:
        if self.poisoned:
            raise RuntimeError("journal requires restart after persistence failure")
        material = self.material(row)
        encoded, identity = canonical(material), material[self.id_field]
        previous = self._ids.get(identity)
        if previous is not None:
            if previous == encoded:
                return False
            raise ValueError("conflicting duplicate journal identity")
        if self.last_time is not None and material[self.time_field] < self.last_time:
            raise ValueError("journal chronological regression")
        unsigned = {"format": "xm-capture-frame/v1", "sequence": self.count + 1, "previous": self.tip, "payload": material}
        digest = sha256(canonical(unsigned).encode()).hexdigest()
        data = (canonical({**unsigned, "digest": digest}) + "\n").encode()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fault("before_append", self.path)
            with self.path.open("ab") as stream:
                stream.write(data[:len(data) // 2])
                stream.flush()
                self.fault("during_append", self.path)
                stream.write(data[len(data) // 2:])
                stream.flush()
                self.fault("after_append_before_fsync", self.path)
                os.fsync(stream.fileno())
            sync_directory(self.path.parent)
            self.fault("after_fsync", self.path)
        except BaseException:
            self.poisoned = True
            raise
        self._accept(material, encoded, digest)
        return True

    @property
    def count(self) -> int:
        return len(self._ids)

    def contains(self, identity: str) -> bool:
        return identity in self._ids

    def latest(self, **criteria: Any) -> Mapping[str, Any] | None:
        found = [row for row in self._rows.values() if all(row.get(k) == v for k, v in criteria.items())]
        return None if not found else dict(max(found, key=lambda row: row[self.time_field]))

    @property
    def rows(self) -> tuple[dict[str, Any], ...]:
        # Detached values prevent callers from altering the accepted ledger.
        return tuple(json.loads(encoded) for encoded in self._ids.values())
