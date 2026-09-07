"""RAPP/1 receipts for skill operations, using the pinned reference implementation."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading

from .rapp1 import rapp as R


_lock = threading.RLock()


def ledger_path(skills_dir):
    skills_dir = Path(skills_dir).resolve()
    scope = hashlib.sha256(str(skills_dir).encode("utf-8")).hexdigest()[:16]
    return skills_dir.parent / ".brainstem_skill_frames" / scope


def _write(path, value):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
            output.write(R.canonical(value) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def emit(skills_dir, action, payload):
    root = ledger_path(skills_dir)
    with _lock:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = root / ".writer"
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as error:
            raise RuntimeError("RAPP/1 skill ledger is busy; verify its writer before retrying.") from error
        try:
            with os.fdopen(lock_fd, "w") as lock_file:
                lock_file.write(str(os.getpid()))
            identity_path = root / "rappid.json"
            if identity_path.exists():
                identity = json.loads(identity_path.read_text(encoding="utf-8"))
                if identity.get("schema") != "rapp/1" or not R.rappid_valid(identity.get("rappid")):
                    raise ValueError("Invalid RAPP/1 skill-ledger identity.")
            else:
                identity = {"schema": "rapp/1", "rappid": R.mint_rappid("local", "brainstem-skills")}
                _write(identity_path, identity)
            frames = root / "frames"
            frames.mkdir(exist_ok=True, mode=0o700)
            head = None
            for path in sorted(frames.glob("*.json")):
                if not path.stem.isdigit():
                    raise ValueError("Unexpected RAPP/1 frame filename.")
                frame = json.loads(path.read_text(encoding="utf-8"))
                ok, step, reason = R.verify_frame(frame, head=head, stream_id_of_record=identity["rappid"])
                if not ok or int(path.stem) != frame["seq"]:
                    raise ValueError(f"RAPP/1 ledger refused at {path.name}: {step}: {reason}")
                head = frame
            utc = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            frame = R.build_frame(
                kind="skill." + action, stream_id=identity["rappid"],
                seq=0 if head is None else head["seq"] + 1, utc=utc, payload=payload,
                prev=None if head is None else head["payload_hash"],
            )
            ok, step, reason = R.verify_frame(frame, head=head, stream_id_of_record=identity["rappid"])
            if not ok:
                raise ValueError(f"RAPP/1 frame refused: {step}: {reason}")
            _write(frames / f"{frame['seq']:020d}.json", frame)
            return frame
        finally:
            lock_path.unlink(missing_ok=True)


def skill_receipt(skills_dir, action, skill):
    raw = skill["markdown"].encode("utf-8")
    return emit(skills_dir, action, {
        "name": skill["name"], "filename": skill["filename"],
        "media_type": "text/markdown", "source_sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    })
