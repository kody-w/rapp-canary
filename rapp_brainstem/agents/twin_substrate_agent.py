"""twin_substrate_agent.py — designate ANY parent, virtual or physical, and give
its twin a substrate: the parent's own record, harvested, indexed, queryable.

ONE file. Stdlib only. Drop into agents/ on any standard brainstem. Touches NO
engine file — brainstem.py, VERSION, soul.md and the rest of the Grail kernel are
never read for write and never modified. This is a cartridge, per Article II.

WHAT THIS ADDS TO THE TWIN CONTRACT
-----------------------------------
`rapp/1-twin` (canon, twin-opus) says a twin is "soul + agents + memory, running
live on whatever model the host provides... what transfers is judgment." That
stands unchanged. Canon also has `parent_rappid` — but that is LINEAGE: which
TWIN this twin descends from, walking back to the rapp species root.

Nothing in canon says what a twin is a twin OF. `kind="place"` hands a physical
parent a VOICE template and no knowledge — roleplay, not a twin.

`rapp/2-twin` is additive. Two new blocks, no field removed, no reader broken:

  parent    — the SUBJECT. Anything virtual or physical can be designated.
              Open vocabulary: person, place, repo, device, org, process,
              vehicle, document, system, animal, account, machine, ...
              An enum would be the same mistake as a fixed twin taxonomy.

  substrate — where the twin's knowledge of that parent comes from. A list of
              sources, each with a registered type, harvested into one normalized
              event stream and indexed. Every parent class uses the SAME engine;
              only the source types differ. A person's substrate is transcripts
              and commits. A building's is photos, inspections and sensor logs.

A twin without a substrate guesses at its parent. A twin with one KNOWS it, and
every answer carries a ptr back to the exact line of evidence.

USAGE (over /chat)
------------------
  "Designate my workflow as a parent and twin it"
    -> TwinSubstrate(action="designate", twin="kody-workflow",
                     parent_class="person", parent_nature="virtual",
                     preset="workflow")
  "Harvest it"        -> TwinSubstrate(action="harvest", twin="kody-workflow")
  "How did I fix the device-code auth hang?"
                      -> TwinSubstrate(action="recall", twin="kody-workflow",
                                       query="device code auth hang")

BULK MODE (outside a request timeout)
-------------------------------------
  python twin_substrate_agent.py harvest kody-workflow
  python twin_substrate_agent.py status  kody-workflow
"""

import csv
from contextlib import contextmanager
import fnmatch
import hashlib
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

try:
    from agents.basic_agent import BasicAgent
except ImportError:  # CLI mode, outside the brainstem
    class BasicAgent:
        def __init__(self, *a, **k): pass


__manifest__ = {
    "schema": "rapp-agent/1.0",
    "name": "@kody-w/twin_substrate_agent",
    "version": "1.0.0",
    "display_name": "Twin Substrate",
    "description": (
        "Designate anything virtual or physical as the parent of a twin, bind the "
        "sources that record it, harvest them into one indexed substrate, and query "
        "it. Every answer cites a pointer back to the original evidence."
    ),
    "author": "kody-w",
    "tags": ["twin", "substrate", "parent", "exhaust", "index", "local-first"],
    "category": "general",
    "quality_tier": "community",
    "requires_env": [],
    "dependencies": ["@rapp/basic_agent"],
    "example_call": "Designate my workflow as a parent and build its twin substrate.",
}

TWIN_SCHEMA = "rapp/2-twin"
ACTIONS = ("designate", "bind", "harvest", "search", "recall", "timeline",
           "status", "list", "inspect", "sources", "open")
_DEFAULT_TEXT_FILE_BYTES = 256 * 1024 * 1024
_DEFAULT_TEXT_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
_DEFAULT_MEDIA_FILE_BYTES = 5 * 1024 * 1024 * 1024
_DEFAULT_MEDIA_SOURCE_BYTES = 20 * 1024 * 1024 * 1024


# ── Paths ───────────────────────────────────────────────────────────────

def _flight_setting(name):
    try:
        flight_path = pathlib.Path(__file__).resolve().parents[2] / "FLIGHT.json"
    except IndexError:
        return None
    if not flight_path.is_file():
        return None
    try:
        flight = json.loads(flight_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read flight configuration: {exc}") from exc
    value = (flight.get("env") or {}).get(name)
    if not value:
        raise RuntimeError(
            f"{flight_path} must define env.{name}; refusing to use daily-driver state."
        )
    return value


def _twins_root():
    configured = os.environ.get("BRAINSTEM_TWINS_ROOT") or _flight_setting(
        "BRAINSTEM_TWINS_ROOT"
    )
    if configured:
        return pathlib.Path(_expand(configured))
    return pathlib.Path.home() / ".brainstem" / "twins"


def _twin_dir(twin):
    return _twins_root() / _slug(twin)


def _ensure_private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path, 0o700)


def _slug(name):
    original = (name or "").strip()
    if not original:
        raise ValueError("Twin name cannot be empty.")
    lowered = original.lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", lowered).strip("-.")
    lossless = bool(slug) and slug == lowered and len(slug) <= 96
    if lossless:
        return slug
    base = (slug or "twin")[:80].rstrip("-.") or "twin"
    suffix = hashlib.sha256(original.encode("utf-8")).hexdigest()[:10]
    return f"{base}-{suffix}"


def _expand(p):
    return os.path.abspath(os.path.expanduser(os.path.expandvars(str(p))))


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(ts):
    """Normalize epoch seconds / ms / ISO string -> ISO-8601 UTC."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        if ts > 1e11:      # milliseconds
            ts = ts / 1000.0
        try:
            return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")
        except Exception:
            return None
    s = str(ts).strip()
    if not s:
        return None
    if re.fullmatch(r"-?\d+(?:\.\d+)?", s):
        try:
            return _iso(float(s))
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return None


# ── Redaction — pointers, never values ──────────────────────────────────
# The substrate is an index of a parent's record. Credential-shaped strings are
# dropped at harvest so they cannot enter the store even once.

_SECRET_PATTERNS = [
    (re.compile(
        r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?"
        r"(?:-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----|\Z)",
        re.DOTALL,
    ), "private-key"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "github-token"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "github-token"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "api-key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws-key-id"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "jwt"),
    (re.compile(
        r"""(?i)(?:"|')?(?:AccountKey|SharedAccessKey|SharedAccessSignature)"""
        r"""(?:"|')?\s*[:=]\s*(?:"[^"\r\n]*"|'[^'\r\n]*'|[^;\s,}]+)"""
    ), "azure-key"),
    (re.compile(
        r"""(?i)(?:[?&]sig=|(?:"|')?sig(?:"|')?\s*[:=]\s*)"""
        r"""(?:"[^"\r\n]*"|'[^'\r\n]*'|[^&\s,;}]+)"""
    ), "azure-sas"),
    (re.compile(
        r"(?i)\bauthorization\s*:\s*(?:bearer|basic)\s+[^\s,\"']+"
    ), "authorization"),
    (re.compile(
        r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"
    ), "authorization"),
    (re.compile(
        r"(?i)\b[A-Z][A-Z0-9+.-]*://[^/\s@]+@[^\s]+"
    ), "uri-credentials"),
    (re.compile(
        r"""(?i)--[A-Z0-9_-]*(?:api[_-]?key|secret|password|passwd|token)"""
        r"""[A-Z0-9_-]*(?:=|\s+)"""
        r"""(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;]+)"""
    ), "cli-secret"),
    (re.compile(
        r"""(?i)(?:^|\s)(?:-u(?:=|\s*)|--user(?:=|\s+))"""
        r"""(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;]+)"""
    ), "cli-secret"),
    (re.compile(
        r"""(?i)(?:"|')?[A-Z0-9_.-]*(?:api[_-]?key|secret|password|passwd|token)"""
        r"""[A-Z0-9_.-]*(?:"|')?\s*[:=]\s*(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;]+)"""
    ), "assignment"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"), "slack-token"),
]
_PRIVATE_KEY_BEGIN = re.compile(
    r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"
)
_PRIVATE_KEY_END = re.compile(
    r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"
)


def _scrub(text):
    if not text:
        return text
    for rx, kind in _SECRET_PATTERNS:
        text = rx.sub(f"[REDACTED:{kind}]", text)
    return text


def _scrub_value(value):
    if isinstance(value, str):
        return _scrub(value)
    if isinstance(value, list):
        return [_scrub_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _scrub_value(item) for key, item in value.items()}
    return value


# ── Store ───────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY,
  ts          TEXT,
  source      TEXT NOT NULL,
  source_type TEXT NOT NULL,
  ref         TEXT,
  kind        TEXT,
  title       TEXT,
  text        TEXT,
  ptr         TEXT,
  origin_path TEXT,
  meta        TEXT,
  dedup       TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_events_ts     ON events(ts);
CREATE INDEX IF NOT EXISTS ix_events_source ON events(source);
CREATE INDEX IF NOT EXISTS ix_events_ref    ON events(ref);

CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
  title, text, content='events', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
  INSERT INTO events_fts(rowid, title, text) VALUES (new.id, new.title, new.text);
END;
CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
  INSERT INTO events_fts(events_fts, rowid, title, text)
  VALUES('delete', old.id, old.title, old.text);
END;

CREATE TABLE IF NOT EXISTS watermarks (
  source TEXT NOT NULL, path TEXT NOT NULL,
  mtime_ns INTEGER, size INTEGER, offset INTEGER DEFAULT 0, seen_utc TEXT,
  realpath TEXT, device INTEGER, inode INTEGER, ctime_ns INTEGER, sha256 TEXT,
  PRIMARY KEY (source, path)
);

CREATE TABLE IF NOT EXISTS substrate_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def _connect(twin):
    d = _twin_dir(twin)
    _ensure_private_dir(_twins_root())
    _ensure_private_dir(d)
    database = d / "substrate.db"
    con = sqlite3.connect(str(database), timeout=30)
    if os.name != "nt":
        os.chmod(database, 0o600)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(_DDL)
    event_columns = {
        row[1] for row in con.execute("PRAGMA table_info(events)").fetchall()
    }
    if "origin_path" not in event_columns:
        con.execute("ALTER TABLE events ADD COLUMN origin_path TEXT")
    con.execute(
        "CREATE INDEX IF NOT EXISTS ix_events_origin "
        "ON events(source, origin_path)"
    )
    schema_version = con.execute(
        "SELECT value FROM substrate_meta WHERE key='schema_version'"
    ).fetchone()
    if not schema_version or schema_version[0] != "3":
        con.execute("DELETE FROM events")
        con.execute("DELETE FROM watermarks")
        con.execute(
            "INSERT INTO substrate_meta(key,value) VALUES('schema_version','3') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        con.commit()
    columns = {
        row[1] for row in con.execute("PRAGMA table_info(watermarks)").fetchall()
    }
    for name, kind in (
        ("realpath", "TEXT"),
        ("device", "INTEGER"),
        ("inode", "INTEGER"),
        ("ctime_ns", "INTEGER"),
        ("sha256", "TEXT"),
    ):
        if name not in columns:
            con.execute(f"ALTER TABLE watermarks ADD COLUMN {name} {kind}")
    _protect_store_files(d)
    return con


def _protect_store_files(directory):
    if os.name == "nt":
        return
    for path in directory.glob("substrate.db*"):
        if path.is_file():
            os.chmod(path, 0o600)


# ── Manifest (additive — never regenerates what a prior author wrote) ────

def _manifest_path(twin):
    return _twin_dir(twin) / "manifest.json"


def _read_manifest(twin):
    p = _manifest_path(twin)
    if not p.exists():
        return {}
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Could not read existing manifest {p}: {exc}") from exc
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Existing manifest {p} is invalid JSON; refusing to overwrite it: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise ValueError(
            f"Existing manifest {p} must contain a JSON object; refusing to overwrite it."
        )
    return manifest


def _write_manifest(twin, man):
    p = _manifest_path(twin)
    _ensure_private_dir(_twins_root())
    _ensure_private_dir(p.parent)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(man, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextmanager
def _manifest_lock(twin):
    path = _twin_dir(twin) / ".manifest.lock"
    _ensure_private_dir(_twins_root())
    _ensure_private_dir(path.parent)
    with open(path, "a+b") as handle:
        if os.name != "nt":
            os.chmod(path, 0o600)
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _validated_parent(manifest, twin):
    parent = manifest.get("parent")
    if parent is None:
        return {}
    if not isinstance(parent, dict):
        raise ValueError(
            f"Existing manifest for '{twin}' has a non-object parent; "
            "refusing to overwrite it."
        )
    addresses = parent.get("address")
    if addresses is not None:
        valid_addresses = (
            isinstance(addresses, list)
            and all(
                isinstance(address, dict)
                and isinstance(address.get("scheme"), str)
                and isinstance(address.get("value"), str)
                for address in addresses
            )
        )
        if not valid_addresses:
            raise ValueError(
                f"Existing manifest for '{twin}' has an invalid parent.address; "
                "refusing to overwrite it."
            )
        if any(
            _scrub(f"{address['scheme']}://{address['value']}")
            != f"{address['scheme']}://{address['value']}"
            for address in addresses
        ):
            raise ValueError(
                f"Existing manifest for '{twin}' contains a credential-bearing "
                "parent address; remove it before continuing."
            )
    return dict(parent)


def _validated_sources(manifest, twin):
    substrate = manifest.get("substrate")
    if substrate is None:
        return []
    if not isinstance(substrate, dict):
        raise ValueError(
            f"Existing manifest for '{twin}' has a non-object substrate; "
            "refusing to overwrite it."
        )
    sources = substrate.get("sources")
    if sources is None:
        return []
    if not isinstance(sources, list):
        raise ValueError(
            f"Existing manifest for '{twin}' has a non-list substrate.sources; "
            "refusing to overwrite it."
        )
    validated = []
    for index, source in enumerate(sources):
        if (
            not isinstance(source, dict)
            or not isinstance(source.get("type"), str)
            or not source["type"].strip()
        ):
            raise ValueError(
                f"Existing manifest for '{twin}' has an invalid source at index "
                f"{index}; refusing to overwrite it."
            )
        if source.get("id") is not None and (
            not isinstance(source["id"], str) or not source["id"].strip()
        ):
            raise ValueError(
                f"Existing manifest for '{twin}' has a non-string source id at "
                f"index {index}; refusing to overwrite it."
            )
        for field in ("root", "path"):
            if source.get(field) is not None and not isinstance(source[field], str):
                raise ValueError(
                    f"Existing manifest for '{twin}' has a non-string {field} "
                    f"at source index {index}; refusing to overwrite it."
                )
        if bool(source.get("root")) == bool(source.get("path")):
            raise ValueError(
                f"Existing manifest for '{twin}' source index {index} must have "
                "exactly one of root or path; refusing to overwrite it."
            )
        configured_path = source.get("root") or source.get("path")
        expanded_path = os.path.expandvars(os.path.expanduser(configured_path))
        if not os.path.isabs(expanded_path):
            raise ValueError(
                f"Existing manifest for '{twin}' source index {index} uses a "
                "relative path; rebind it with a stable absolute boundary."
            )
        boundary = source.get("boundary")
        if (
            not isinstance(boundary, dict)
            or not isinstance(boundary.get("realpath"), str)
            or not isinstance(boundary.get("device"), int)
            or not isinstance(boundary.get("inode"), int)
            or boundary.get("kind") != "directory"
        ):
            raise ValueError(
                f"Existing manifest for '{twin}' source index {index} has no "
                "stable boundary identity; rebind it before harvesting."
            )
        if boundary.get("target") is not None and (
            not isinstance(boundary["target"], str)
            or not os.path.isabs(boundary["target"])
        ):
            raise ValueError(
                f"Existing manifest for '{twin}' source index {index} has an "
                "invalid exact target boundary; rebind it before harvesting."
            )
        if source["type"] == "git_estate":
            repositories = source.get("repositories")
            if not isinstance(repositories, list) or not repositories:
                raise ValueError(
                    f"Existing manifest for '{twin}' source index {index} has no "
                    "pinned Git repositories; rebind it before harvesting."
                )
            for repository in repositories:
                if not isinstance(repository, dict) or any(
                    not isinstance(repository.get(key), dict)
                    for key in ("worktree", "marker", "git_dir", "common_dir")
                ):
                    raise ValueError(
                        f"Existing manifest for '{twin}' source index {index} has "
                        "invalid Git identity metadata; rebind it before harvesting."
                    )
        for field in ("globs", "exts"):
            if source.get(field) is not None and (
                not isinstance(source[field], list)
                or any(not isinstance(item, str) for item in source[field])
            ):
                raise ValueError(
                    f"Existing manifest for '{twin}' has an invalid {field} "
                    f"at source index {index}; refusing to overwrite it."
                )
        validated.append(dict(source))
    return validated


# ── Source type registry ────────────────────────────────────────────────
# A harvester yields normalized event dicts. Adding a parent class never means
# touching the engine — it means registering a source type here.

HARVESTERS = {}


def harvester(name):
    def deco(fn):
        HARVESTERS[name] = fn
        return fn
    return deco


def _ev(ts, kind, title, text, ref=None, ptr=None, origin_path=None, **meta):
    return {
        "ts": _iso(ts),
        "kind": kind,
        "title": title or "",
        "text": text or "",
        "ref": ref,
        "ptr": ptr,
        "origin_path": origin_path,
        "meta": meta,
    }


def _file_ptr(path, line, version):
    return f"{path}:{line}@{version[:16]}"


def _split_file_ptr(ptr):
    match = re.match(r"^(.*):(\d+)(?:@([0-9a-f]{16,64}))?$", ptr or "")
    if not match:
        return ptr, 1, None
    return match.group(1), int(match.group(2)), match.group(3)


def _wm_get(con, source, path):
    row = con.execute(
        "SELECT mtime_ns,size,offset,realpath,device,inode,ctime_ns,sha256 "
        "FROM watermarks WHERE source=? AND path=?",
        (source, path),
    ).fetchone()
    return row or (None, None, 0, None, None, None, None, None)


def _wm_set(con, source, path, stat_result, offset=0, digest=None):
    con.execute(
        "INSERT INTO watermarks("
        "source,path,mtime_ns,size,offset,seen_utc,realpath,device,inode,"
        "ctime_ns,sha256"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source,path) DO UPDATE SET "
        "mtime_ns=excluded.mtime_ns,size=excluded.size,offset=excluded.offset,"
        "seen_utc=excluded.seen_utc,realpath=excluded.realpath,"
        "device=excluded.device,inode=excluded.inode,"
        "ctime_ns=excluded.ctime_ns,sha256=excluded.sha256",
        (
            source,
            path,
            stat_result.st_mtime_ns,
            stat_result.st_size,
            offset,
            _now(),
            os.path.realpath(path),
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_ctime_ns,
            digest,
        ),
    )


def _watermark_matches(con, source, path, stat_result, digest):
    mtime, size, _, realpath, device, inode, ctime, stored_digest = _wm_get(
        con, source, path
    )
    return (
        mtime == stat_result.st_mtime_ns
        and size == stat_result.st_size
        and realpath == os.path.realpath(path)
        and device == stat_result.st_dev
        and inode == stat_result.st_ino
        and ctime == stat_result.st_ctime_ns
        and stored_digest == digest
    )


def _mark(con, source, path):
    snapshot, stat_result, digest = _snapshot_file(
        path, max_bytes=_DEFAULT_TEXT_FILE_BYTES
    )
    snapshot.close()
    _wm_set(con, source, path, stat_result, digest=digest)


def _remove_file_events(con, source, path, remove_watermark=True):
    con.execute(
        "DELETE FROM events WHERE source=? AND origin_path=?",
        (source, path),
    )
    if remove_watermark:
        con.execute(
            "DELETE FROM watermarks WHERE source=? AND path=?",
            (source, path),
        )


def _replace_file_events(con, source, path):
    _remove_file_events(con, source, path, remove_watermark=False)


def _reconcile_source_files(con, source, current_paths):
    current = {os.path.abspath(path) for path in current_paths}
    previous = {
        row[0]
        for row in con.execute(
            "SELECT path FROM watermarks WHERE source=?", (source,)
        ).fetchall()
    }
    for missing in previous - current:
        _remove_file_events(con, source, missing)


def _assert_safe_evidence_path(
    path, boundary, require_file=True, boundary_identity=None
):
    candidate = os.path.abspath(path)
    allowed = os.path.abspath(boundary)
    if not os.path.exists(allowed):
        raise ValueError(f"Evidence boundary no longer exists: {allowed}")
    if os.path.islink(allowed):
        raise ValueError(f"Evidence boundary became a symlink: {allowed}")
    if boundary_identity:
        boundary_stat = os.stat(allowed, follow_symlinks=False)
        if (
            os.path.realpath(allowed) != boundary_identity.get("realpath")
            or boundary_stat.st_dev != boundary_identity.get("device")
            or boundary_stat.st_ino != boundary_identity.get("inode")
        ):
            raise ValueError(
                f"Evidence boundary identity changed; rebind this source: {allowed}"
            )

    boundary_is_dir = os.path.isdir(allowed)
    if boundary_is_dir:
        try:
            relative = os.path.relpath(candidate, allowed)
        except ValueError as exc:
            raise ValueError(
                f"Evidence path is outside its bound source: {candidate}"
            ) from exc
        if relative == os.pardir or relative.startswith(os.pardir + os.sep):
            raise ValueError(f"Evidence path is outside its bound source: {candidate}")
        cursor = allowed
        for part in pathlib.Path(relative).parts:
            cursor = os.path.join(cursor, part)
            if os.path.islink(cursor):
                raise ValueError(f"Refusing symlinked evidence path: {candidate}")
    elif os.path.normcase(candidate) != os.path.normcase(allowed):
        raise ValueError(f"Evidence path does not match its bound file: {candidate}")
    elif os.path.islink(candidate):
        raise ValueError(f"Refusing symlinked evidence path: {candidate}")

    real_candidate = os.path.realpath(candidate)
    real_allowed = os.path.realpath(allowed)
    if boundary_is_dir:
        try:
            common = os.path.commonpath([real_candidate, real_allowed])
        except ValueError as exc:
            raise ValueError(
                f"Evidence path resolves outside its bound source: {candidate}"
            ) from exc
        if os.path.normcase(common) != os.path.normcase(real_allowed):
            raise ValueError(
                f"Evidence path resolves outside its bound source: {candidate}"
            )
    elif os.path.normcase(real_candidate) != os.path.normcase(real_allowed):
        raise ValueError(
            f"Evidence path resolves outside its bound file: {candidate}"
        )

    if require_file and not os.path.isfile(candidate):
        raise ValueError(f"Evidence path is not a regular file: {candidate}")
    if not require_file and not os.path.isdir(candidate):
        raise ValueError(f"Evidence path is not a directory: {candidate}")
    return candidate


def _source_boundary(source, pointer_path):
    boundary = source.get("boundary") or {}
    configured = boundary.get("realpath")
    if not configured:
        raise ValueError(
            f"Source {source.get('id') or source.get('type')} has no path boundary."
        )
    target = boundary.get("target")
    if target and os.path.normcase(os.path.abspath(pointer_path)) != os.path.normcase(
        target
    ):
        raise ValueError(
            f"Evidence path does not match its bound source file: {pointer_path}"
        )
    return configured


def _open_nofollow(path):
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"Could not open evidence safely: {exc}") from exc
    return descriptor


def _same_file_identity(left, right):
    return (
        left.st_mtime_ns == right.st_mtime_ns
        and left.st_size == right.st_size
        and left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_ctime_ns == right.st_ctime_ns
    )


def _run_bounded(args, timeout, max_bytes, env=None):
    process = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    stdout = bytearray()
    stderr = bytearray()
    overflow = []
    lock = threading.Lock()

    def drain(stream, target, limit, label):
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            with lock:
                remaining = max(0, limit + 1 - len(target))
                target.extend(chunk[:remaining])
                if len(target) > limit:
                    overflow.append(label)
                    try:
                        process.kill()
                    except OSError:
                        pass
                    return

    threads = [
        threading.Thread(
            target=drain,
            args=(process.stdout, stdout, max_bytes, "stdout"),
            daemon=True,
        ),
        threading.Thread(
            target=drain,
            args=(process.stderr, stderr, 64 * 1024, "stderr"),
            daemon=True,
        ),
    ]
    for thread in threads:
        thread.start()
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    finally:
        for thread in threads:
            thread.join()
    if overflow:
        raise ValueError(
            f"Command {overflow[0]} exceeded its safety limit."
        )
    error = bytes(stderr).decode("utf-8", errors="replace")
    if returncode != 0:
        raise ValueError(error.strip() or f"Command exited {returncode}.")
    return bytes(stdout).decode("utf-8", errors="replace")


def _snapshot_file(path, max_bytes, budget=None):
    descriptor = _open_nofollow(path)
    snapshot = tempfile.TemporaryFile()
    try:
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if before.st_size > max_bytes:
                raise ValueError(
                    f"Evidence file exceeds the {max_bytes:,}-byte limit: {path}"
                )
            if budget is not None:
                if budget["used"] + before.st_size > budget["limit"]:
                    raise ValueError(
                        f"Source exceeds the {budget['limit']:,}-byte harvest limit."
                    )
                budget["used"] += before.st_size
            remaining = before.st_size
            digest = hashlib.sha256()
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError(f"Evidence shrank while being copied: {path}")
                snapshot.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            if handle.read(1):
                raise ValueError(f"Evidence grew while being copied: {path}")
            after = os.fstat(handle.fileno())
            if not _same_file_identity(before, after):
                raise ValueError(f"Evidence changed while being copied: {path}")
        snapshot.seek(0)
        return snapshot, before, digest.hexdigest()
    except Exception:
        snapshot.close()
        raise


@contextmanager
def _verified_harvest_reader(
    con, source, path, boundary, boundary_identity, max_bytes, budget=None
):
    candidate = _assert_safe_evidence_path(
        path, boundary, boundary_identity=boundary_identity
    )
    realpath = os.path.realpath(candidate)
    snapshot, before, digest = _snapshot_file(candidate, max_bytes, budget)
    try:
        yield snapshot, before, digest
    except Exception:
        raise
    else:
        _assert_safe_evidence_path(
            candidate,
            boundary,
            boundary_identity=boundary_identity,
        )
        current = os.stat(candidate, follow_symlinks=False)
        if (
            not _same_file_identity(before, current)
            or os.path.realpath(candidate) != realpath
        ):
            raise ValueError(
                f"Evidence changed while it was being harvested: {candidate}"
            )
        _wm_set(con, source, candidate, before, digest=digest)
    finally:
        snapshot.close()


@contextmanager
def _open_verified_binary(con, source, path, version=None):
    (
        mtime,
        size,
        _,
        _,
        device,
        inode,
        ctime,
        digest,
    ) = _wm_get(con, source, path)
    snapshot, before, current_digest = _snapshot_file(path, max_bytes=size or 1)
    try:
        matches = (
            mtime == before.st_mtime_ns
            and size == before.st_size
            and device == before.st_dev
            and inode == before.st_ino
            and ctime == before.st_ctime_ns
        )
        if not matches:
            raise ValueError(
                f"Evidence changed since it was harvested: {path}. Re-harvest first."
            )
        if not digest or current_digest != digest:
            raise ValueError(
                f"Evidence content changed since it was harvested: {path}. "
                "Re-harvest first."
            )
        if version and not digest.startswith(version):
            raise ValueError(
                f"Evidence pointer is for an older version of {path}. "
                "Search again after harvesting."
            )
        yield snapshot
    finally:
        snapshot.close()


def _read_physical_line(handle, capture_limit):
    captured = bytearray()
    saw_data = False
    truncated = False
    while True:
        part = handle.readline(8192)
        if not part:
            return bytes(captured), truncated, not saw_data
        saw_data = True
        room = max(0, capture_limit - len(captured))
        if room:
            captured.extend(part[:room])
        if len(part) > room:
            truncated = True
        if part.endswith(b"\n"):
            return bytes(captured), truncated, False


def _iter_text_lines(handle, max_line_bytes=1024 * 1024):
    line_number = 0
    while True:
        raw, truncated, missing = _read_physical_line(handle, max_line_bytes)
        if missing:
            return
        line_number += 1
        if truncated:
            raise ValueError(
                f"Evidence line {line_number} exceeds the "
                f"{max_line_bytes:,}-byte safety limit."
            )
        yield line_number, raw, raw.decode("utf-8", errors="replace")


def _matches_any(relative_path, patterns):
    posix_path = pathlib.PurePath(relative_path).as_posix()
    return any(
        pathlib.PurePath(posix_path).match(pattern)
        or fnmatch.fnmatch(posix_path, pattern)
        or (
            pattern.startswith("**/")
            and fnmatch.fnmatch(posix_path, pattern[3:])
        )
        for pattern in patterns
    )


def _iter_source_files(root, patterns, boundary_identity, max_files, max_entries):
    _assert_safe_evidence_path(
        root,
        root,
        require_file=False,
        boundary_identity=boundary_identity,
    )
    stack = [root]
    matched = 0
    visited = 0
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                visited += 1
                if visited > max_entries:
                    raise ValueError(
                        f"Source traversal exceeded the {max_entries:,}-entry limit."
                    )
                if entry.is_symlink():
                    raise ValueError(f"Refusing symlinked source entry: {entry.path}")
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                relative = os.path.relpath(entry.path, root)
                if not _matches_any(relative, patterns):
                    continue
                if matched >= max_files:
                    raise ValueError(
                        f"Source contains more than the {max_files:,}-file limit."
                    )
                matched += 1
                yield entry.path


def _text_of(content):
    """Flatten an Anthropic-style message content field to plain text."""
    if isinstance(content, str):
        return content
    out = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, str):
                out.append(b)
            elif isinstance(b, dict):
                if b.get("type") == "text" and b.get("text"):
                    out.append(b["text"])
                elif b.get("type") == "tool_use":
                    out.append(f"[tool:{b.get('name','?')}]")
                elif b.get("type") == "thinking":
                    continue
    return "\n".join(out)


@harvester("claude_transcripts")
def _h_claude(con, src, emit):
    """Claude Code session JSONL. Indexes turn text + metadata, not raw tool
    payloads — those stay on disk and are reachable through the ptr."""
    root = _expand(src.get("root", "~/.claude/projects"))
    limit = _bounded_int(
        src.get("limit_files"), 10_000, 100_000
    )
    max_file_bytes = _bounded_int(
        src.get("max_file_bytes"), _DEFAULT_TEXT_FILE_BYTES, 2 * 1024 * 1024 * 1024
    )
    budget = {
        "used": 0,
        "limit": _bounded_int(
            src.get("max_total_bytes"),
            _DEFAULT_TEXT_SOURCE_BYTES,
            20 * 1024 * 1024 * 1024,
        ),
    }
    files = list(_iter_source_files(
        root,
        ["**/*.jsonl"],
        src["boundary"],
        max_files=limit,
        max_entries=_bounded_int(src.get("max_entries"), 250_000, 1_000_000),
    ))
    _reconcile_source_files(con, src["id"], files)
    n = 0
    for fp in files:
        with _verified_harvest_reader(
            con,
            src["id"],
            fp,
            root,
            src["boundary"],
            max_file_bytes,
            budget,
        ) as (
            handle, stat_result, version
        ):
            if _watermark_matches(
                con, src["id"], fp, stat_result, version
            ):
                continue
            _replace_file_events(con, src["id"], fp)
            for ln, raw, line in _iter_text_lines(handle):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = d.get("type")
                if t not in ("user", "assistant"):
                    continue
                msg = d.get("message") or {}
                body = _text_of(msg.get("content"))
                if not body or len(body) < 12:
                    continue
                ref = (
                    d.get("cwd")
                    or d.get("slug")
                    or os.path.basename(os.path.dirname(fp))
                )
                emit(_ev(
                    d.get("timestamp"),
                    f"turn.{t}",
                    d.get("aiTitle") or body.strip().split("\n")[0],
                    body,
                    ref=ref,
                    ptr=_file_ptr(fp, ln, version),
                    origin_path=fp,
                    session=d.get("sessionId"),
                    branch=d.get("gitBranch"),
                    cwd=d.get("cwd"),
                ))
                n += 1
    return n


@harvester("prompt_history")
def _h_prompts(con, src, emit):
    """The intent stream: what was asked for, in the parent's own words."""
    fp = _expand(src.get("path", "~/.claude/history.jsonl"))
    if not os.path.exists(fp):
        _remove_file_events(con, src["id"], fp)
        return 0
    boundary = src["boundary"]["realpath"]
    _assert_safe_evidence_path(
        fp, boundary, boundary_identity=src["boundary"]
    )
    n = 0
    with _verified_harvest_reader(
        con,
        src["id"],
        fp,
        boundary,
        src["boundary"],
        _bounded_int(
            src.get("max_file_bytes"),
            _DEFAULT_TEXT_FILE_BYTES,
            2 * 1024 * 1024 * 1024,
        ),
    ) as (
        handle, stat_result, version
    ):
        if _watermark_matches(con, src["id"], fp, stat_result, version):
            return 0
        _replace_file_events(con, src["id"], fp)
        for ln, raw, line in _iter_text_lines(handle):
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            disp = (d.get("display") or "").strip()
            if not disp or disp.startswith("/clear"):
                continue
            emit(_ev(
                d.get("timestamp"),
                "prompt",
                disp,
                disp,
                ref=d.get("project"),
                ptr=_file_ptr(fp, ln, version),
                origin_path=fp,
                session=d.get("sessionId"),
            ))
            n += 1
    return n


@harvester("git_estate")
def _h_git(con, src, emit):
    """Shipped truth. One root of repos, or a single repo."""
    root = _expand(src.get("root", "."))
    since = src.get("since", "1 year ago")
    max_commits = _bounded_int(src.get("max_commits"), 5000, 50_000)
    _assert_safe_evidence_path(
        root,
        root,
        require_file=False,
        boundary_identity=src["boundary"],
    )
    repositories = src["repositories"]
    n = 0
    for repository in repositories:
        _validate_git_repo(repository)
        repo = repository["worktree"]["realpath"]
        _assert_safe_evidence_path(
            repo,
            root,
            require_file=False,
            boundary_identity=src["boundary"],
        )
        out = _run_bounded(
            _git_command(repository, [
                "log", "--no-show-signature", "--no-textconv", "--no-ext-diff",
                f"--since={since}", "--all",
                "--no-merges", f"--max-count={max_commits}", "--format=%H",
            ]),
            timeout=60,
            max_bytes=4 * 1024 * 1024,
            env=_safe_git_env(),
        )
        name = os.path.basename(repo)
        for sha in out.splitlines():
            sha = sha.strip()
            if not sha:
                continue
            if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha):
                raise ValueError(f"Git returned an invalid commit id: {sha!r}")
            raw_commit = _run_bounded(
                _git_command(repository, ["cat-file", "commit", sha]),
                timeout=30,
                max_bytes=1024 * 1024,
                env=_safe_git_env(),
            )
            commit = _parse_commit_object(raw_commit)
            emit(_ev(
                commit["timestamp"],
                "commit",
                commit["subject"],
                commit["message"],
                ref=name,
                ptr=f"{repo}#{sha}",
                sha=sha,
                author=commit["author"],
            ))
            n += 1
        _validate_git_repo(repository)
    return n


@harvester("filesystem")
def _h_fs(con, src, emit):
    """Text-bearing files under a root: notes, docs, inspections, reports."""
    root = _expand(src.get("root", "."))
    globs = src.get("globs") or ["**/*.md"]
    maxb = _bounded_int(src.get("max_bytes"), 400_000, 5_000_000)
    budget = {
        "used": 0,
        "limit": _bounded_int(
            src.get("max_total_bytes"),
            _DEFAULT_TEXT_SOURCE_BYTES,
            20 * 1024 * 1024 * 1024,
        ),
    }
    n = 0
    files = list(_iter_source_files(
        root,
        globs,
        src["boundary"],
        max_files=_bounded_int(src.get("limit_files"), 10_000, 100_000),
        max_entries=_bounded_int(src.get("max_entries"), 250_000, 1_000_000),
    ))
    _reconcile_source_files(con, src["id"], files)
    for fp in files:
        with _verified_harvest_reader(
            con,
            src["id"],
            fp,
            root,
            src["boundary"],
            maxb,
            budget,
        ) as (
            handle, stat_result, version
        ):
            if _watermark_matches(
                con, src["id"], fp, stat_result, version
            ):
                continue
            _replace_file_events(con, src["id"], fp)
            inside_private_key = False
            for line_number, raw, line in _iter_text_lines(handle):
                if inside_private_key:
                    if _PRIVATE_KEY_END.search(line):
                        inside_private_key = False
                    continue
                if _PRIVATE_KEY_BEGIN.search(line):
                    emit(_ev(
                        stat_result.st_mtime,
                        "document",
                        f"{os.path.basename(fp)}:{line_number}",
                        "[REDACTED:private-key]\n",
                        ref=os.path.relpath(fp, root),
                        ptr=_file_ptr(fp, line_number, version),
                        origin_path=fp,
                    ))
                    inside_private_key = not bool(_PRIVATE_KEY_END.search(line))
                    n += 1
                    continue
                if not line.strip():
                    continue
                emit(_ev(
                    stat_result.st_mtime,
                    "document",
                    f"{os.path.basename(fp)}:{line_number}",
                    line,
                    ref=os.path.relpath(fp, root),
                    ptr=_file_ptr(fp, line_number, version),
                    origin_path=fp,
                ))
                n += 1
    return n


@harvester("shell_history")
def _h_shell(con, src, emit):
    fp = _expand(src.get("path", "~/.zsh_history"))
    if not os.path.exists(fp):
        _remove_file_events(con, src["id"], fp)
        return 0
    boundary = src["boundary"]["realpath"]
    _assert_safe_evidence_path(
        fp, boundary, boundary_identity=src["boundary"]
    )
    n = 0
    rx = re.compile(r"^: (\d+):\d+;(.*)$")
    with _verified_harvest_reader(
        con,
        src["id"],
        fp,
        boundary,
        src["boundary"],
        _bounded_int(
            src.get("max_file_bytes"),
            _DEFAULT_TEXT_FILE_BYTES,
            2 * 1024 * 1024 * 1024,
        ),
    ) as (
        handle, stat_result, version
    ):
        if _watermark_matches(con, src["id"], fp, stat_result, version):
            return 0
        _replace_file_events(con, src["id"], fp)
        for ln, raw, line in _iter_text_lines(handle):
            line = line.rstrip("\n")
            m = rx.match(line)
            ts, cmd = (int(m.group(1)), m.group(2)) if m else (None, line)
            cmd = cmd.strip()
            if len(cmd) < 4:
                continue
            emit(_ev(
                ts,
                "command",
                cmd,
                cmd,
                ptr=_file_ptr(fp, ln, version),
                origin_path=fp,
            ))
            n += 1
    return n


@harvester("csv_timeseries")
def _h_csv(con, src, emit):
    """PHYSICAL parents: sensor readings, meter logs, inspection rows.
    Any CSV with a timestamp column becomes substrate."""
    root = _expand(src.get("root", "."))
    tscol = src.get("ts_column")
    if src["boundary"].get("target") and not os.path.exists(root):
        _remove_file_events(con, src["id"], root)
        return 0
    paths = [root] if os.path.isfile(root) else list(_iter_source_files(
        root,
        [src.get("glob", "**/*.csv")],
        src["boundary"],
        max_files=_bounded_int(src.get("limit_files"), 10_000, 100_000),
        max_entries=_bounded_int(src.get("max_entries"), 250_000, 1_000_000),
    ))
    _reconcile_source_files(con, src["id"], paths)
    budget = {
        "used": 0,
        "limit": _bounded_int(
            src.get("max_total_bytes"),
            _DEFAULT_TEXT_SOURCE_BYTES,
            20 * 1024 * 1024 * 1024,
        ),
    }
    n = 0
    for fp in paths:
        boundary = src["boundary"]["realpath"]
        _assert_safe_evidence_path(
            fp, boundary, boundary_identity=src["boundary"]
        )
        with _verified_harvest_reader(
            con,
            src["id"],
            fp,
            boundary,
            src["boundary"],
            _bounded_int(
                src.get("max_file_bytes"),
                _DEFAULT_TEXT_FILE_BYTES,
                2 * 1024 * 1024 * 1024,
            ),
            budget,
        ) as (
            handle, stat_result, version
        ):
            if _watermark_matches(
                con, src["id"], fp, stat_result, version
            ):
                continue
            _replace_file_events(con, src["id"], fp)
            text_lines = (
                line for _ln, _raw, line in _iter_text_lines(handle)
            )
            reader = csv.DictReader(text_lines)
            columns = reader.fieldnames or []
            timestamp_column = tscol or next(
                (column for column in columns if column and re.search(
                    r"(?i)time|date|ts\b", column
                )),
                None,
            )
            while True:
                start_line = reader.line_num + 1
                try:
                    row = next(reader)
                except StopIteration:
                    break
                body = "; ".join(f"{key}={value}" for key, value in row.items() if value)
                if not body:
                    continue
                emit(_ev(
                    row.get(timestamp_column) if timestamp_column else stat_result.st_mtime,
                    "reading",
                    body,
                    body,
                    ref=os.path.basename(fp),
                    ptr=_file_ptr(fp, start_line, version),
                    origin_path=fp,
                ))
                n += 1
    return n


@harvester("media")
def _h_media(con, src, emit):
    """PHYSICAL parents: photographs of a place, a device, a vehicle, a build.
    Indexes the observation (what/when/where on disk), not pixels."""
    root = _expand(src.get("root", "."))
    exts = tuple(e.lower() for e in (src.get("exts") or
                 [".jpg", ".jpeg", ".png", ".heic", ".mov", ".mp4", ".pdf"]))
    budget = {
        "used": 0,
        "limit": _bounded_int(
            src.get("max_total_bytes"),
            _DEFAULT_MEDIA_SOURCE_BYTES,
            100 * 1024 * 1024 * 1024,
        ),
    }
    n = 0
    files = list(_iter_source_files(
        root,
        [
            pattern
            for extension in exts
            for pattern in (f"**/*{extension}", f"**/*{extension.upper()}")
        ],
        src["boundary"],
        max_files=_bounded_int(src.get("limit_files"), 10_000, 100_000),
        max_entries=_bounded_int(src.get("max_entries"), 250_000, 1_000_000),
    ))
    _reconcile_source_files(con, src["id"], files)
    for fp in files:
        nm = os.path.basename(fp)
        with _verified_harvest_reader(
            con,
            src["id"],
            fp,
            root,
            src["boundary"],
            _bounded_int(
                src.get("max_file_bytes"),
                _DEFAULT_MEDIA_FILE_BYTES,
                20 * 1024 * 1024 * 1024,
            ),
            budget,
        ) as (_handle, stat_result, version):
            if _watermark_matches(
                con, src["id"], fp, stat_result, version
            ):
                continue
            _replace_file_events(con, src["id"], fp)
            rel = os.path.relpath(fp, root)
            # The folder path is the human's own labelling — real signal.
            body = (
                f"{nm} in {os.path.dirname(rel) or '.'} "
                f"({stat_result.st_size} bytes)"
            )
            emit(_ev(
                stat_result.st_mtime,
                "observation",
                nm,
                body,
                ref=rel,
                ptr=_file_ptr(fp, 1, version),
                origin_path=fp,
                bytes=stat_result.st_size,
            ))
            n += 1
    return n


@harvester("jsonl")
def _h_jsonl(con, src, emit):
    """Generic escape hatch: any JSONL, with configurable field mapping."""
    configured_path = src.get("path")
    if not configured_path:
        return 0
    fp = _expand(configured_path)
    if not os.path.exists(fp):
        _remove_file_events(con, src["id"], fp)
        return 0
    boundary = src["boundary"]["realpath"]
    _assert_safe_evidence_path(
        fp, boundary, boundary_identity=src["boundary"]
    )
    fts, ftx, fti = src.get("ts_field", "timestamp"), src.get("text_field", "text"), \
        src.get("title_field", "title")
    n = 0
    with _verified_harvest_reader(
        con,
        src["id"],
        fp,
        boundary,
        src["boundary"],
        _bounded_int(
            src.get("max_file_bytes"),
            _DEFAULT_TEXT_FILE_BYTES,
            2 * 1024 * 1024 * 1024,
        ),
    ) as (
        handle, stat_result, version
    ):
        if _watermark_matches(con, src["id"], fp, stat_result, version):
            return 0
        _replace_file_events(con, src["id"], fp)
        for ln, raw, line in _iter_text_lines(handle):
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            body = d.get(ftx) or json.dumps(d)[:2000]
            emit(_ev(
                d.get(fts),
                src.get("kind", "record"),
                str(d.get(fti) or body),
                str(body),
                ptr=_file_ptr(fp, ln, version),
                origin_path=fp,
            ))
            n += 1
    return n


# ── Presets — starting substrates for common parent classes ─────────────
# Not a taxonomy. Convenience only; any source list can be built by hand.

PRESETS = {
    "workflow": [
        {"type": "claude_transcripts", "root": "~/.claude/projects"},
        {"type": "prompt_history", "path": "~/.claude/history.jsonl"},
        {"type": "filesystem", "root": "~/.claude/projects", "globs": ["**/memory/*.md"]},
        {"type": "shell_history", "path": "~/.zsh_history"},
    ],
    "repo": [
        {"type": "git_estate", "root": "."},
        {"type": "filesystem", "root": ".", "globs": ["**/*.md"]},
    ],
    "estate": [
        {"type": "git_estate", "root": "~/Documents/GitHub", "since": "1 year ago"},
    ],
    "place": [
        {"type": "media", "root": "."},
        {"type": "filesystem", "root": ".", "globs": ["**/*.md", "**/*.txt"]},
        {"type": "csv_timeseries", "root": "."},
    ],
    "device": [
        {"type": "csv_timeseries", "root": "."},
        {"type": "filesystem", "root": ".", "globs": ["**/*.log", "**/*.md"]},
    ],
}

_ROOT_SOURCE_DEFAULTS = {
    "claude_transcripts": "~/.claude/projects",
    "git_estate": ".",
    "filesystem": ".",
    "csv_timeseries": ".",
    "media": ".",
}
_PATH_SOURCE_DEFAULTS = {
    "prompt_history": "~/.claude/history.jsonl",
    "shell_history": "~/.zsh_history",
    "jsonl": None,
}


def _safe_git_env():
    env = {
        key: os.environ[key]
        for key in (
            "PATH",
            "HOME",
            "TMPDIR",
            "TMP",
            "TEMP",
            "SystemRoot",
            "ComSpec",
            "PATHEXT",
        )
        if key in os.environ
    }
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_ALLOW_PROTOCOL": "",
        "GIT_PAGER": "cat",
        "GIT_OPTIONAL_LOCKS": "0",
        "LC_ALL": "C",
    })
    return env


def _path_identity(path, kind, digest=None):
    if os.path.islink(path):
        raise ValueError(f"Refusing symlinked Git metadata path: {path}")
    stat_result = os.stat(path, follow_symlinks=False)
    if kind == "directory" and not os.path.isdir(path):
        raise ValueError(f"Expected Git directory: {path}")
    if kind == "file" and not os.path.isfile(path):
        raise ValueError(f"Expected Git metadata file: {path}")
    identity = {
        "path": os.path.abspath(path),
        "realpath": os.path.realpath(path),
        "device": stat_result.st_dev,
        "inode": stat_result.st_ino,
        "ctime_ns": stat_result.st_ctime_ns,
        "kind": kind,
    }
    if digest:
        identity["sha256"] = digest
    return identity


def _read_small_file(path, max_bytes=16 * 1024):
    snapshot, _stat, digest = _snapshot_file(path, max_bytes=max_bytes)
    try:
        data = snapshot.read(max_bytes + 1)
    finally:
        snapshot.close()
    if len(data) > max_bytes:
        raise ValueError(f"Git metadata file exceeds {max_bytes:,} bytes: {path}")
    return data.decode("utf-8", errors="strict"), digest


def _pin_git_repo(path):
    worktree = os.path.realpath(path)
    worktree_identity = _path_identity(worktree, "directory")
    marker_path = os.path.join(worktree, ".git")
    if os.path.isdir(marker_path):
        marker = _path_identity(marker_path, "directory")
        git_dir = marker["realpath"]
    elif os.path.isfile(marker_path):
        marker_text, marker_digest = _read_small_file(marker_path)
        match = re.fullmatch(r"\s*gitdir:\s*(.+?)\s*", marker_text)
        if not match:
            raise ValueError(f"Malformed Git worktree marker: {marker_path}")
        marker = _path_identity(marker_path, "file", marker_digest)
        target = match.group(1)
        if not os.path.isabs(target):
            target = os.path.join(worktree, target)
        git_dir = os.path.realpath(target)
    else:
        raise ValueError(f"Not a Git worktree: {worktree}")
    git_dir_identity = _path_identity(git_dir, "directory")

    common_marker = os.path.join(git_dir, "commondir")
    if os.path.isfile(common_marker):
        common_text, common_digest = _read_small_file(common_marker)
        common_path = common_text.strip()
        if not os.path.isabs(common_path):
            common_path = os.path.join(git_dir, common_path)
        common_dir = os.path.realpath(common_path)
        common_marker_identity = _path_identity(
            common_marker, "file", common_digest
        )
    else:
        common_dir = git_dir
        common_marker_identity = None
    common_dir_identity = _path_identity(common_dir, "directory")
    return {
        "worktree": worktree_identity,
        "marker": marker,
        "git_dir": git_dir_identity,
        "common_dir": common_dir_identity,
        "common_marker": common_marker_identity,
    }


def _identity_matches(identity):
    path = identity["path"]
    try:
        current = _path_identity(path, identity["kind"])
    except (OSError, ValueError):
        return False
    if (
        current["realpath"] != identity["realpath"]
        or current["device"] != identity["device"]
        or current["inode"] != identity["inode"]
    ):
        return False
    if identity["kind"] == "file" and current["ctime_ns"] != identity["ctime_ns"]:
        return False
    if identity.get("sha256"):
        try:
            _, digest = _read_small_file(path)
        except (OSError, UnicodeError, ValueError):
            return False
        if digest != identity["sha256"]:
            return False
    return True


def _validate_git_repo(repository):
    for key in ("worktree", "marker", "git_dir", "common_dir"):
        if not _identity_matches(repository[key]):
            raise ValueError(
                f"Pinned Git {key} identity changed for "
                f"{repository['worktree']['path']}; rebind this source."
            )
    common_marker = repository.get("common_marker")
    if common_marker and not _identity_matches(common_marker):
        raise ValueError(
            f"Pinned Git common-dir marker changed for "
            f"{repository['worktree']['path']}; rebind this source."
        )
    current = _pin_git_repo(repository["worktree"]["path"])
    if current["git_dir"]["realpath"] != repository["git_dir"]["realpath"]:
        raise ValueError("Pinned Git directory target changed; rebind this source.")
    if current["common_dir"]["realpath"] != repository["common_dir"]["realpath"]:
        raise ValueError("Pinned Git common directory changed; rebind this source.")


def _discover_git_repositories(root, max_repos, max_entries):
    marker = os.path.join(root, ".git")
    if os.path.exists(marker):
        return [_pin_git_repo(root)]
    repositories = []
    visited = 0
    with os.scandir(root) as entries:
        for entry in entries:
            visited += 1
            if visited > max_entries:
                raise ValueError(
                    f"Git estate discovery exceeded the {max_entries:,}-entry limit."
                )
            if entry.is_symlink():
                raise ValueError(f"Refusing symlinked repository entry: {entry.path}")
            if not entry.is_dir(follow_symlinks=False):
                continue
            if os.path.exists(os.path.join(entry.path, ".git")):
                repositories.append(_pin_git_repo(entry.path))
                if len(repositories) >= max_repos:
                    break
    if not repositories:
        raise ValueError(f"No Git repositories found under {root}")
    return repositories


def _git_command(repository, command):
    return [
        "git",
        "--no-pager",
        "--no-replace-objects",
        f"--git-dir={repository['git_dir']['realpath']}",
        f"--work-tree={repository['worktree']['realpath']}",
        "-c",
        "log.showSignature=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        *command,
    ]


def _parse_commit_object(raw):
    headers, separator, message = raw.partition("\n\n")
    if not separator:
        raise ValueError("Malformed Git commit object.")
    author_line = next(
        (line for line in headers.splitlines() if line.startswith("author ")),
        None,
    )
    if not author_line:
        raise ValueError("Git commit object has no author header.")
    match = re.match(r"^author (.+?) <[^>]*> (-?\d+) [+-]\d{4}$", author_line)
    if not match:
        raise ValueError("Malformed Git author header.")
    subject = message.splitlines()[0] if message.splitlines() else "(no subject)"
    return {
        "author": match.group(1),
        "timestamp": _iso(int(match.group(2))),
        "subject": subject,
        "message": message.rstrip("\n"),
    }


def _nearest_git_root(path):
    try:
        root = _run_bounded(
            ["git", "--no-pager", "-C", path, "rev-parse", "--show-toplevel"],
            timeout=10,
            max_bytes=16 * 1024,
            env=_safe_git_env(),
        ).strip()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ValueError(
            "The repo source default requires a Git checkout; provide an explicit "
            f"root for an estate instead ({exc})."
        ) from exc
    if not root:
        raise ValueError("Git did not return a repository root.")
    return root


def _materialize_source(source):
    source = dict(source)
    source_type = source.get("type")
    if source.get("root") and source.get("path"):
        raise ValueError("A source must use root or path, never both.")
    if source_type in _ROOT_SOURCE_DEFAULTS:
        if source.get("path"):
            raise ValueError(f"Source type {source_type!r} requires root, not path.")
        configured = source.get("root") or _ROOT_SOURCE_DEFAULTS[source_type]
        if source_type == "git_estate" and configured in (".", "./"):
            configured = _nearest_git_root(os.getcwd())
        source["root"] = os.path.realpath(_expand(configured))
        source.pop("path", None)
    elif source_type in _PATH_SOURCE_DEFAULTS:
        if source.get("root"):
            raise ValueError(f"Source type {source_type!r} requires path, not root.")
        configured = source.get("path") or _PATH_SOURCE_DEFAULTS[source_type]
        if not configured:
            raise ValueError(f"Source type {source_type!r} requires path.")
        source["path"] = os.path.realpath(_expand(configured))
        source.pop("root", None)
    else:
        configured = source.get("root") or source.get("path")
        if not configured:
            raise ValueError(
                f"Source type {source_type!r} needs a stable root or path boundary."
            )
        field = "root" if source.get("root") else "path"
        source[field] = os.path.realpath(_expand(configured))
    configured = source.get("root") or source.get("path")
    if not os.path.exists(configured):
        raise ValueError(f"Source boundary does not exist: {configured}")
    if source.get("root") and not os.path.isdir(configured):
        if source_type != "csv_timeseries":
            raise ValueError(f"Source root is not a directory: {configured}")
    if source.get("path") and not os.path.isfile(configured):
        raise ValueError(f"Source path is not a regular file: {configured}")
    boundary_path = (
        configured if os.path.isdir(configured) else os.path.dirname(configured)
    )
    stat_result = os.stat(boundary_path, follow_symlinks=False)
    source["boundary"] = {
        "realpath": os.path.realpath(boundary_path),
        "device": stat_result.st_dev,
        "inode": stat_result.st_ino,
        "kind": "directory",
    }
    if not os.path.isdir(configured):
        source["boundary"]["target"] = configured
    if source_type == "git_estate":
        source["repositories"] = _discover_git_repositories(
            source["root"],
            _bounded_int(source.get("max_repos"), 1000, 10_000),
            _bounded_int(source.get("max_entries"), 250_000, 1_000_000),
        )
    source.pop("id", None)
    source["id"] = _sid(source)
    return source


# ── Operations ──────────────────────────────────────────────────────────

def _normalize_addresses(address):
    addresses = address if isinstance(address, list) else ([address] if address else [])
    normalized = []
    for item in addresses:
        if isinstance(item, dict):
            scheme, value = item.get("scheme"), item.get("value")
            if not isinstance(scheme, str) or not isinstance(value, str):
                raise ValueError(
                    "Each parent address object needs string scheme and value fields."
                )
            if _scrub(f"{scheme}://{value}") != f"{scheme}://{value}":
                raise ValueError(
                    "Parent addresses cannot contain credential-shaped data."
                )
            normalized.append({"scheme": scheme, "value": value})
        elif isinstance(item, str):
            if _scrub(item) != item:
                raise ValueError(
                    "Parent addresses cannot contain credential-shaped data."
                )
            if "://" in item:
                scheme, value = item.split("://", 1)
                normalized.append({"scheme": scheme, "value": value})
            elif os.path.exists(_expand(item)):
                normalized.append({"scheme": "file", "value": _expand(item)})
            else:
                normalized.append({"scheme": "name", "value": item})
        else:
            raise ValueError("Each parent address must be a string or address object.")
    return normalized


def op_designate(twin, parent_class="thing", parent_nature="virtual",
                 display_name=None, address=None, preset=None, note=None, **_):
    """Designate a parent and give its twin a substrate.

    parent_class is an OPEN vocabulary. person, place, repo, device, org,
    process, vehicle, document, system, animal, account — or anything else.
    A closed enum here would recreate the exact limitation this fixes.
    """
    parent_nature = (parent_nature or "virtual").strip().lower()
    parent_class = (parent_class or "thing").strip() or "thing"
    if parent_nature not in ("virtual", "physical", "hybrid"):
        return (f"parent_nature must be virtual, physical or hybrid "
                f"(got {parent_nature!r}). A physical parent is a real-world "
                f"thing — a building, a machine, a vehicle, a body of land.")
    if preset and preset not in PRESETS:
        return f"Unknown preset {preset!r}. Available: {', '.join(sorted(PRESETS))}"
    slug = _slug(twin)
    normalized_addresses = _normalize_addresses(address)
    with _manifest_lock(slug):
        man = _read_manifest(slug)
        parent = _validated_parent(man, slug)
        sources = _validated_sources(man, slug)
        substrate = dict(man.get("substrate") or {})

        parent.update({
            "nature": parent_nature,
            "class": parent_class,
            "display_name": display_name or _display(slug),
            "address": normalized_addresses or parent.get("address") or [],
            "designated_utc": parent.get("designated_utc") or _now(),
        })
        if note:
            parent["note"] = note

        if preset:
            base = _expand(normalized_addresses[0]["value"]) if (
                normalized_addresses
                and normalized_addresses[0].get("scheme") == "file"
            ) else None
            if not base and preset == "repo":
                base = _nearest_git_root(os.getcwd())
            for source in PRESETS[preset]:
                source = dict(source)
                if base:
                    for key in ("root", "path"):
                        if source.get(key) in (".", "./"):
                            source[key] = base
                        elif key in source and str(source[key]).startswith("./"):
                            source[key] = os.path.join(
                                base, str(source[key])[2:]
                            )
                source = _materialize_source(source)
                if not any(
                    _logical_source_key(item) == _logical_source_key(source)
                    for item in sources
                ):
                    sources.append(source)

        # rapp/1-twin fields are preserved; rapp/2-twin is additive.
        man.setdefault("name", slug)
        man.setdefault("display_name", parent["display_name"])
        man.setdefault("created_utc", _now())
        man.setdefault("kind", "RAPP Twin")
        man["schema"] = TWIN_SCHEMA
        man["parent"] = parent
        substrate.update({
            "store": str(_twin_dir(slug) / "substrate.db"),
            "sources": sources,
            "engine": "@kody-w/twin_substrate_agent",
        })
        man["substrate"] = substrate
        man.setdefault(
            "what_a_twin_is",
            "soul + agents + memory, running live on whatever model the host "
            "provides. Not a copy of a model - weights do not move. What "
            "transfers is judgment.",
        )
        man["parent_contract"] = (
            "`parent` is the SUBJECT this twin is a twin OF - anything virtual or "
            "physical may be designated. Distinct from `parent_rappid`, which is "
            "LINEAGE (which twin this one descends from). Both may be present."
        )

        lines = [
            f"Designated parent for twin '{slug}'.",
            f"  parent : {parent['display_name']}  "
            f"[{parent_nature}/{parent_class}]",
        ]
        for item in parent["address"]:
            lines.append(f"  address: {item.get('scheme')}://{item.get('value')}")
        lines.append(
            f"  sources: {len(sources)} bound"
            + (f" (preset '{preset}')" if preset else "")
        )
        lines.append(f"  store  : {substrate['store']}")
        for source in sources:
            lines.append(
                f"     - {source['type']}: "
                f"{source.get('root') or source.get('path') or ''}"
            )
        lines.append("")
        lines.append("Nothing is indexed yet. Harvest to give the twin its knowledge:")
        lines.append(f"  TwinSubstrate(action='harvest', twin='{slug}')")

        _connect(slug).close()
        _write_manifest(slug, man)
    return "\n".join(lines)


def _display(slug):
    return " ".join(w.capitalize() for w in re.split(r"[-_.]+", slug) if w)


def _sid(s):
    key = json.dumps(
        {
            k: v
            for k, v in s.items()
            if k not in ("id", "boundary", "repositories")
        },
        sort_keys=True,
    )
    return s["type"] + ":" + hashlib.sha256(key.encode()).hexdigest()[:8]


def _logical_source_key(source):
    return (
        source.get("type"),
        source.get("root") or source.get("path"),
    )


def op_bind(twin, source_type=None, root=None, path=None, **kw):
    """Bind one more source to an existing parent's substrate."""
    slug = _slug(twin)
    if source_type not in HARVESTERS:
        return (f"Unknown source_type {source_type!r}.\nRegistered: "
                + ", ".join(sorted(HARVESTERS)))
    src = {"type": source_type}
    if root:
        src["root"] = root
    if path:
        src["path"] = path
    for k, v in kw.items():
        if k in ("globs", "exts", "since", "max_repos", "limit_files",
                 "ts_column", "glob", "kind", "max_bytes", "ts_field",
                 "text_field", "title_field", "max_commits",
                 "max_file_bytes", "max_total_bytes", "max_entries"):
            src[k] = v
    src = _materialize_source(src)
    src = _validated_sources({"substrate": {"sources": [src]}}, slug)[0]
    with _manifest_lock(slug):
        man = _read_manifest(slug)
        previous_manifest = json.loads(json.dumps(man))
        parent = _validated_parent(man, slug)
        if not parent:
            return (f"Twin '{slug}' has no designated parent yet. "
                    f"Run action='designate' first.")
        sources = _validated_sources(man, slug)
        substrate = dict(man.get("substrate") or {})
        substrate.setdefault("store", str(_twin_dir(slug) / "substrate.db"))
        substrate.setdefault("engine", "@kody-w/twin_substrate_agent")
        existing_index = next(
            (
                index
                for index, item in enumerate(sources)
                if _logical_source_key(item) == _logical_source_key(src)
            ),
            None,
        )
        rebuild_source_id = None
        if existing_index is not None:
            existing = sources[existing_index]
            src["id"] = existing.get("id") or _sid(existing)
            if existing == src:
                return f"That exact source is already bound to '{slug}'."
            sources[existing_index] = src
            rebuild_source_id = src["id"]
            verb = "Rebound"
        else:
            sources.append(src)
            verb = "Bound"
        substrate["sources"] = sources
        man["substrate"] = substrate
        if rebuild_source_id:
            con = _connect(slug)
            try:
                con.execute("BEGIN IMMEDIATE")
                con.execute("DELETE FROM events WHERE source=?", (rebuild_source_id,))
                con.execute(
                    "DELETE FROM watermarks WHERE source=?", (rebuild_source_id,)
                )
                _write_manifest(slug, man)
                con.commit()
            except Exception:
                con.rollback()
                _write_manifest(slug, previous_manifest)
                raise
            finally:
                con.close()
        else:
            _write_manifest(slug, man)
    return (f"{verb} {source_type} -> {src.get('root') or src.get('path')} to '{slug}'. "
            f"{len(sources)} sources total. Harvest to index it.")


def op_harvest(twin, source_type=None, limit_files=None, **_):
    slug = _slug(twin)
    man = _read_manifest(slug)
    sources = _validated_sources(man, slug)
    if not sources:
        return f"Twin '{slug}' has no bound sources. Designate a parent first."
    con = _connect(slug)
    total, report, t0 = 0, [], time.time()
    for src in sources:
        if source_type and src["type"] != source_type:
            continue
        fn = HARVESTERS.get(src["type"])
        if not fn:
            report.append(f"  ! {src['type']}: no harvester registered")
            continue
        src = dict(src)
        src["id"] = src.get("id") or _sid(src)
        if limit_files:
            src["limit_files"] = _bounded_int(limit_files, 0, 100_000)
        batch = []
        batch_bytes = 0
        inserted = 0

        def emit(ev, _b=batch):
            nonlocal batch_bytes, inserted
            if not isinstance(ev, dict):
                raise ValueError("A substrate harvester emitted a non-object event.")
            ev["ts"] = _iso(ev.get("ts"))
            for field in ("text", "title", "ref", "kind"):
                ev[field] = _scrub(ev.get(field))
            if isinstance(ev.get("title"), str):
                ev["title"] = ev["title"][:500]
            ev["meta"] = _scrub_value(ev.get("meta") or {})
            if ev.get("ptr") and _scrub(ev["ptr"]) != ev["ptr"]:
                raise ValueError(
                    "An evidence pointer contains credential-shaped data; "
                    "refusing to persist it."
                )
            event_bytes = sum(
                len(str(ev.get(field) or "").encode("utf-8"))
                for field in (
                    "ts", "kind", "title", "text", "ref", "ptr",
                    "origin_path", "meta",
                )
            )
            if _b and (
                len(_b) >= 1000 or batch_bytes + event_bytes > 4 * 1024 * 1024
            ):
                inserted += _flush(con, src, _b)
                batch_bytes = 0
            _b.append(ev)
            batch_bytes += event_bytes
            if len(_b) >= 1000 or batch_bytes >= 4 * 1024 * 1024:
                inserted += _flush(con, src, _b)
                batch_bytes = 0

        con.execute("SAVEPOINT source_harvest")
        try:
            n = fn(con, src, emit)
            inserted += _flush(con, src, batch)
        except Exception as exc:
            con.execute("ROLLBACK TO source_harvest")
            con.execute("RELEASE source_harvest")
            report.append(f"  ! {src['type']}: {type(exc).__name__}: {exc}")
            continue
        con.execute("RELEASE source_harvest")
        con.commit()
        _protect_store_files(_twin_dir(slug))
        added = inserted
        total += added
        report.append(
            f"  + {src['type']:<20} {added:>7,} new / {n:>7,} scanned"
        )
    rows = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    con.close()
    dt = time.time() - t0
    return ("\n".join([f"Harvested substrate for '{slug}' in {dt:.1f}s:"] + report
                      + [f"  = {total:,} new  |  {rows:,} total events in store"]))


def _flush(con, src, batch):
    if not batch:
        return 0
    rows = []
    for ev in batch:
        dedup = hashlib.sha256(
            f"{src['id']}|{ev.get('ptr')}|{ev.get('ts')}|{(ev.get('text') or '')[:200]}"
            .encode()).hexdigest()
        rows.append((ev.get("ts"), src["id"], src["type"], ev.get("ref"),
                     ev.get("kind"), ev.get("title"), ev.get("text"),
                     ev.get("ptr"), ev.get("origin_path"),
                     json.dumps(ev.get("meta") or {}), dedup))
    cursor = con.executemany(
        "INSERT OR IGNORE INTO events("
        "ts,source,source_type,ref,kind,title,text,ptr,origin_path,meta,dedup"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    batch.clear()
    return max(0, cursor.rowcount)


def _fts_query(q):
    """Build a safe FTS5 MATCH expression from free text."""
    toks = re.findall(r"[A-Za-z0-9_]{2,}", q or "")
    if not toks:
        return None
    return " OR ".join(f'"{t}"' for t in toks)


def _bounded_int(value, default, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def op_search(twin, query=None, limit=12, source_type=None, since=None,
              ref=None, **_):
    slug = _slug(twin)
    if not _read_manifest(slug):
        return f"No twin '{slug}'. Use action='list' to see what exists."
    m = _fts_query(query)
    if not m:
        return "Give me a query with at least one word."
    con = _connect(slug)
    sql = ("SELECT e.ts,e.source_type,e.ref,e.kind,e.title,e.text,e.ptr,"
           " bm25(events_fts) AS rank"
           " FROM events_fts JOIN events e ON e.id=events_fts.rowid"
           " WHERE events_fts MATCH ?")
    args = [m]
    if source_type:
        sql += " AND e.source_type=?"; args.append(source_type)
    if since:
        normalized_since = _iso(since)
        if not normalized_since:
            con.close()
            return f"Invalid since timestamp: {since!r}."
        sql += " AND e.ts>=?"; args.append(normalized_since)
    if ref:
        sql += " AND e.ref LIKE ?"; args.append(f"%{ref}%")
    sql += " ORDER BY rank LIMIT ?"
    args.append(_bounded_int(limit, 12, 100))
    try:
        rows = con.execute(sql, args).fetchall()
    except sqlite3.OperationalError as e:
        con.close()
        return f"Query failed: {e}"
    con.close()
    if not rows:
        return f"Nothing in {slug}'s substrate matches {query!r}."
    out = [f"{len(rows)} hit(s) in {slug}'s substrate for {query!r}:", ""]
    for ts, st, ref_, kind, title, text, ptr, _r in rows:
        out.append(f"[{(ts or '?')[:19]}] {st}/{kind}  {ref_ or ''}")
        out.append(f"  {(title or '').strip()[:180]}")
        snip = re.sub(r"\s+", " ", (text or "")).strip()
        if len(snip) > 240:
            snip = snip[:240] + "..."
        if snip and snip[:120] != (title or "").strip()[:120]:
            out.append(f"  {snip}")
        out.append(f"  ptr: {ptr}")
        out.append("")
    out.append("Open any ptr with action='open' to read the original evidence.")
    return "\n".join(out)


def op_recall(twin, query=None, limit=8, **_):
    """'How was this handled before?' — the answer plus its evidence."""
    slug = _slug(twin)
    if not _read_manifest(slug):
        return f"No twin '{slug}'. Use action='list' to see what exists."
    m = _fts_query(query)
    if not m:
        return "Give me something to recall."
    con = _connect(slug)
    rows = con.execute(
        "SELECT e.ts,e.source_type,e.ref,e.kind,e.title,e.text,e.ptr"
        " FROM events_fts JOIN events e ON e.id=events_fts.rowid"
        " WHERE events_fts MATCH ? ORDER BY bm25(events_fts) LIMIT ?",
        (m, _bounded_int(limit, 8, 40) * 3)).fetchall()
    con.close()
    if not rows:
        return f"{slug}'s substrate has no record of {query!r}."
    by_ref = {}
    for r in rows:
        by_ref.setdefault(r[2] or "?", []).append(r)
    out = [f"What {slug}'s parent actually did about {query!r}:", ""]
    for ref_, group in list(by_ref.items())[:_bounded_int(limit, 8, 40)]:
        span = sorted(x[0] or "" for x in group)
        out.append(f"### {ref_}   ({len(group)} events, {span[0][:10]} -> {span[-1][:10]})")
        for ts, st, _rf, kind, title, text, ptr in group[:3]:
            out.append(f"  [{(ts or '?')[:10]}] {kind}: {(title or '')[:150]}")
            out.append(f"     ptr: {ptr}")
        out.append("")
    return "\n".join(out)


def op_timeline(twin, since=None, until=None, ref=None, limit=40, **_):
    slug = _slug(twin)
    if not _read_manifest(slug):
        return f"No twin '{slug}'. Use action='list' to see what exists."
    con = _connect(slug)
    sql = ("SELECT substr(ts,1,10) d, source_type, COUNT(*), "
           " MIN(title) FROM events WHERE ts IS NOT NULL")
    args = []
    if since:
        normalized_since = _iso(since)
        if not normalized_since:
            con.close()
            return f"Invalid since timestamp: {since!r}."
        sql += " AND ts>=?"; args.append(normalized_since)
    if until:
        normalized_until = _iso(until)
        if not normalized_until:
            con.close()
            return f"Invalid until timestamp: {until!r}."
        sql += " AND ts<=?"; args.append(normalized_until)
    if ref:
        sql += " AND ref LIKE ?"; args.append(f"%{ref}%")
    sql += " GROUP BY d, source_type ORDER BY d DESC LIMIT ?"
    args.append(_bounded_int(limit, 40, 366))
    rows = con.execute(sql, args).fetchall()
    con.close()
    if not rows:
        return f"No timeline for '{slug}' in that window."
    out = [f"{slug} substrate timeline" + (f" (ref~{ref})" if ref else ""), ""]
    cur = None
    for d, st, c, sample in rows:
        if d != cur:
            out.append(f"{d}"); cur = d
        out.append(f"   {st:<20} {c:>6,}   {(sample or '')[:90]}")
    return "\n".join(out)


def op_status(twin=None, **_):
    if not twin:
        return op_list()
    slug = _slug(twin)
    man = _read_manifest(slug)
    if not man:
        return f"No twin '{slug}'. Use action='list' to see what exists."
    p = man.get("parent") or {}
    con = _connect(slug)
    total = con.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    per = con.execute("SELECT source_type, COUNT(*), MIN(ts), MAX(ts) FROM events"
                      " GROUP BY source_type ORDER BY 2 DESC").fetchall()
    refs = con.execute("SELECT ref, COUNT(*) c FROM events WHERE ref IS NOT NULL"
                       " GROUP BY ref ORDER BY c DESC LIMIT 8").fetchall()
    con.close()
    db = _twin_dir(slug) / "substrate.db"
    size = db.stat().st_size if db.exists() else 0
    out = [f"Twin: {man.get('display_name') or slug}   [schema {man.get('schema')}]"]
    if p:
        out.append(f"Parent: {p.get('display_name')}  "
                   f"[{p.get('nature')}/{p.get('class')}]  since {p.get('designated_utc','?')[:10]}")
        for a in p.get("address") or []:
            address = f"{a.get('scheme') or ''}://{a.get('value') or ''}"
            out.append(f"   address: {_scrub(address)}")
    else:
        out.append("Parent: NONE DESIGNATED — this twin has no subject.")
    if man.get("parent_rappid"):
        out.append(f"Lineage: descends from {man['parent_rappid'][:40]}...")
    out.append(f"Substrate: {total:,} events, {size/1e6:.1f} MB")
    for st, c, mn, mx in per:
        out.append(f"   {st:<20} {c:>8,}   {(mn or '?')[:10]} -> {(mx or '?')[:10]}")
    if refs:
        out.append("Most-recorded refs:")
        for r, c in refs:
            out.append(f"   {c:>7,}  {r}")
    return "\n".join(out)


def op_list(**_):
    root = _twins_root()
    if not root.exists():
        return "No twins on this device yet."
    out = ["Twins on this device:", ""]
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        man = _read_manifest(d.name)
        p = man.get("parent") or {}
        db = d / "substrate.db"
        n = 0
        if db.exists():
            try:
                c = sqlite3.connect(str(db))
                n = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                c.close()
            except Exception:
                pass
        if p:
            tag = f"{p.get('nature')}/{p.get('class')}: {p.get('display_name')}"
        else:
            tag = "no parent designated"
        out.append(f"  {d.name:<28} {tag}")
        out.append(f"  {'':<28} schema {man.get('schema','?')}, "
                   f"{n:,} substrate events")
    out.append("")
    out.append("Anything virtual or physical can be designated a parent.")
    return "\n".join(out)


def op_sources(**_):
    out = ["Registered substrate source types:", ""]
    doc = {
        "claude_transcripts": "Claude Code session JSONL (virtual: a person's work)",
        "prompt_history": "prompt stream — intent in the parent's own words",
        "git_estate": "commits across a root of repos, or one repo",
        "filesystem": "text files by glob — notes, docs, reports, inspections",
        "shell_history": "commands actually run",
        "csv_timeseries": "PHYSICAL: sensor/meter/inspection rows with a timestamp",
        "media": "PHYSICAL: photos/video/PDF of a place, device, vehicle, build",
        "jsonl": "generic JSONL with configurable field mapping",
    }
    for t in sorted(HARVESTERS):
        out.append(f"  {t:<20} {doc.get(t,'')}")
    out += ["", "Presets: " + ", ".join(sorted(PRESETS)),
            "", "A new parent class never needs an engine change — it needs a "
            "source type registered here."]
    return "\n".join(out)


def op_inspect(twin, **_):
    slug = _slug(twin)
    man = _read_manifest(slug)
    if not man:
        return f"No twin '{slug}'."
    safe = _scrub_value(man)
    for address in ((safe.get("parent") or {}).get("address") or []):
        if not isinstance(address, dict):
            continue
        rendered = f"{address.get('scheme') or ''}://{address.get('value') or ''}"
        if _scrub(rendered) != rendered:
            address["scheme"] = "redacted"
            address["value"] = "[REDACTED:uri-credentials]"
    return json.dumps(safe, indent=2)


_MAX_OPEN_BYTES = 64 * 1024


def _read_bounded_window(handle, line_number, line_count):
    for _ in range(line_number - 1):
        _, _, missing = _read_physical_line(handle, 0)
        if missing:
            return None

    remaining = _MAX_OPEN_BYTES
    lines = []
    target = None
    truncated = False
    for index in range(line_count):
        line, line_truncated, missing = _read_physical_line(handle, remaining)
        if missing:
            break
        if index == 0:
            target = line
        lines.append(line)
        remaining -= len(line)
        truncated = truncated or line_truncated
        if truncated or remaining <= 0:
            break
    if target is None:
        return None
    chunk = b"".join(lines).decode("utf-8", errors="replace")
    target_text = target.decode("utf-8", errors="replace")
    return target_text, chunk, truncated


def op_open(twin, ptr=None, context=6, **_):
    """Read the original evidence a ptr points at."""
    if not ptr:
        return "Give me a ptr from a search result."
    slug = _slug(twin)
    manifest = _read_manifest(slug)
    if not manifest:
        return f"No twin '{slug}'. Use action='list' to see what exists."
    sources = _validated_sources(manifest, slug)
    con = _connect(slug)
    event = con.execute(
        "SELECT source,source_type,title,text,meta FROM events "
        "WHERE ptr=? ORDER BY id DESC LIMIT 1",
        (ptr,),
    ).fetchone()
    if not event:
        con.close()
        return (
            f"Pointer is not indexed in '{slug}'; refusing to read an arbitrary "
            "local path. Search the twin first and open a returned ptr."
        )
    source_id, source_type, title, stored_text, meta_json = event
    source = next(
        (
            item
            for item in sources
            if (item.get("id") or _sid(item)) == source_id
        ),
        None,
    )
    if not source:
        con.close()
        return (
            f"Pointer source {source_id!r} is no longer bound to '{slug}'; "
            "refusing to open it."
        )
    if "#" in ptr and ":" not in ptr.rsplit("#", 1)[-1]:
        repo, sha = ptr.rsplit("#", 1)
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", sha):
            con.close()
            return f"Could not open {ptr}: invalid Git object id."
        try:
            repository = next(
                item for item in source["repositories"]
                if os.path.normcase(item["worktree"]["realpath"])
                == os.path.normcase(os.path.realpath(repo))
            )
            _validate_git_repo(repository)
            boundary = _source_boundary(source, repo)
            _assert_safe_evidence_path(
                repo,
                boundary,
                require_file=False,
                boundary_identity=source["boundary"],
            )
            _run_bounded(
                _git_command(repository, ["cat-file", "-e", f"{sha}^{{commit}}"]),
                timeout=30,
                max_bytes=4 * 1024,
                env=_safe_git_env(),
            )
            output = _run_bounded(
                _git_command(repository, ["cat-file", "commit", sha]),
                timeout=30,
                max_bytes=1024 * 1024,
                env=_safe_git_env(),
            )
            _validate_git_repo(repository)
        except (
            OSError,
            StopIteration,
            ValueError,
            subprocess.SubprocessError,
        ) as exc:
            con.close()
            return f"Could not open {ptr}: {exc}"
        con.close()
        return _scrub(output)[:4000]
    fp, ln, version = _split_file_ptr(ptr)
    try:
        boundary = _source_boundary(source, fp)
        _assert_safe_evidence_path(
            fp, boundary, boundary_identity=source["boundary"]
        )
        with _open_verified_binary(con, source_id, fp, version=version) as handle:
            if source_type == "media":
                try:
                    metadata = json.loads(meta_json or "{}")
                except json.JSONDecodeError:
                    metadata = {}
                media_output = (
                    f"--- {ptr}\n"
                    "Media evidence is not decoded as text.\n"
                    f"title: {_scrub(title or '')}\n"
                    f"observation: {_scrub(stored_text or '')}\n"
                    f"metadata: {json.dumps(_scrub_value(metadata), sort_keys=True)}"
                )[:4000]
                window = None
            else:
                context = _bounded_int(context, 6, 100)
                media_output = None
                window = _read_bounded_window(handle, ln, context)
    except (OSError, ValueError) as exc:
        con.close()
        return f"Could not open {ptr}: {exc}"
    con.close()
    if media_output is not None:
        return media_output
    if window is None:
        return f"Pointer line no longer exists: {fp}:{ln}"
    target_line, chunk, truncated = window
    if fp.endswith(".jsonl"):
        try:
            d = json.loads(target_line)
            body = _text_of((d.get("message") or {}).get("content"))
            if not body:
                # Not a transcript line (e.g. history.jsonl uses `display`).
                body = next((str(d[k]) for k in ("display", "text", "content",
                                                 "prompt", "summary")
                             if d.get(k)), "")
            if not body:
                body = json.dumps(d, indent=2)[:2000]
            hdr = " ".join(f"{k}={d[k]}" for k in ("type", "timestamp", "cwd",
                                                   "project", "sessionId")
                           if d.get(k))
            chunk = f"{hdr}\n\n{body}"
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
    if truncated:
        chunk += "\n[TRUNCATED]"
    return f"--- {fp}:{ln}\n{_scrub(chunk)[:4000]}"


OPS = {"designate": op_designate, "bind": op_bind, "harvest": op_harvest,
       "search": op_search, "recall": op_recall, "timeline": op_timeline,
       "status": op_status, "list": op_list, "inspect": op_inspect,
       "sources": op_sources, "open": op_open}


# ── Agent surface ───────────────────────────────────────────────────────

class TwinSubstrateAgent(BasicAgent):
    def __init__(self):
        self.name = "TwinSubstrate"
        self.metadata = {
            "name": self.name,
            "description": __manifest__["description"] + (
                " Actions: designate (make anything virtual or physical the parent "
                "of a twin), bind (add a source), harvest (index it), search, "
                "recall, timeline, status, list, sources, inspect, open."),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(ACTIONS),
                               "description": "What to do."},
                    "twin": {"type": "string",
                             "description": "Twin name, e.g. 'kody-workflow'."},
                    "parent_class": {"type": "string",
                                     "description": "OPEN vocabulary: person, place, "
                                     "repo, device, org, process, vehicle, document, "
                                     "system, or anything else."},
                    "parent_nature": {"type": "string",
                                      "enum": ["virtual", "physical", "hybrid"]},
                    "display_name": {"type": "string"},
                    "address": {"type": "string",
                                "description": "Where the parent lives: a path, URL, "
                                "geo coordinate, serial number."},
                    "preset": {"type": "string",
                               "description": "Starter source set: "
                               + ", ".join(sorted(PRESETS))},
                    "source_type": {"type": "string",
                                    "description": "For bind/harvest: "
                                    + ", ".join(sorted(HARVESTERS))},
                    "root": {"type": "string"},
                    "path": {"type": "string"},
                    "query": {"type": "string"},
                    "ref": {"type": "string"},
                    "since": {"type": "string"},
                    "until": {"type": "string"},
                    "ptr": {"type": "string"},
                    "limit": {"type": "integer"},
                    "context": {"type": "integer"},
                    "globs": {"type": "array", "items": {"type": "string"}},
                    "exts": {"type": "array", "items": {"type": "string"}},
                    "glob": {"type": "string"},
                    "max_bytes": {"type": "integer"},
                    "max_repos": {"type": "integer"},
                    "max_commits": {"type": "integer"},
                    "max_file_bytes": {"type": "integer"},
                    "max_total_bytes": {"type": "integer"},
                    "max_entries": {"type": "integer"},
                    "limit_files": {"type": "integer"},
                    "ts_column": {"type": "string"},
                    "ts_field": {"type": "string"},
                    "text_field": {"type": "string"},
                    "title_field": {"type": "string"},
                    "kind": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["action"],
            },
        }
        super().__init__()

    def perform(self, **kwargs):
        kwargs.pop("user_guid", None)
        action = (kwargs.pop("action", "") or "").strip().lower()
        fn = OPS.get(action)
        if not fn:
            return (f"Unknown action {action!r}. One of: {', '.join(ACTIONS)}")
        if action in ("designate", "bind", "harvest", "search", "recall",
                      "timeline", "inspect", "open") and not kwargs.get("twin"):
            return f"action='{action}' needs a twin name."
        try:
            return fn(**kwargs)
        except TypeError as e:
            return f"Bad arguments for '{action}': {e}"
        except Exception as e:
            return f"{action} failed: {type(e).__name__}: {e}"


# ── CLI — bulk harvest outside a request timeout ────────────────────────

def _main(argv):
    if len(argv) < 2:
        print(__doc__)
        print("\nActions: " + ", ".join(ACTIONS))
        return 0
    action, rest = argv[1], argv[2:]
    fn = OPS.get(action)
    if not fn:
        print(f"Unknown action {action!r}. One of: {', '.join(ACTIONS)}")
        return 2
    kw = {}
    pos = []
    for a in rest:
        if a.startswith("--") and "=" in a:
            k, v = a[2:].split("=", 1)
            kw[k] = v
        else:
            pos.append(a)
    if pos and "twin" not in kw and action not in ("sources", "list"):
        kw["twin"] = pos[0]
        pos = pos[1:]
    if pos and action in ("search", "recall") and "query" not in kw:
        kw["query"] = " ".join(pos)
    if pos and action == "open" and "ptr" not in kw:
        kw["ptr"] = pos[0]
    print(fn(**kw))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
