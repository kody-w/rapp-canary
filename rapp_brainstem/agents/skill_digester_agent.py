"""Skill Digester — feed the brainstem a file; it digests it or spits it back out.

Drop any of three things into the feed folder and the brainstem gains the
capability on your next message:

    agent.py            a singleton agent — taken as-is
    toasted SKILL.md    carries an rci capsule — the agent.py inside it is
                        extracted and checksum-verified
    raw SKILL.md        prose — toasted in real time, then materialised

Anything else is spat back out into feed/rejected/ with a reason, because a
silent no-op is worse than a refusal.

WHY THIS IS AN AGENT AND NOT PART OF brainstem.py

It was part of brainstem.py, for about six hours on 2026-07-25 — 183 lines of
it — before being reverted. Article I of the constitution says the brainstem is
"a loader + an LLM loop + a response splitter. That's it." Article XXVI rejects
any change loading responsibility into brainstem.py that a *_agent.py could
serve.

This file is the proof that a *_agent.py can serve it. `load_agents()` runs on
every /chat, and every agent's __init__ runs with it, so __init__ is already a
per-turn hook. Nothing needed to be added to the kernel to get one.

WHY A FED SKILL BECOMES AN agent.py INSTEAD OF STAYING A SKILL

Formats are how a capability arrives. agent.py is what it is.

If a .md stayed resident in agents/ in its own shape, the brainstem would carry
a second loader, a second lifecycle, and a second thing to reason about when
something misbehaves — the exact accretion Article I exists to prevent, moved
one level outward. So digestion always terminates in one runtime form. The fed
file is never modified; a new agent.py is written beside it.

WHY IT WILL NOT CLAIM PROSE IS CODE

A toasted skill carrying a real agent payload materialises that payload and is
marked EXEC. A skill that is only prose materialises an agent whose perform()
returns its own instructions, and is marked SPEC — it does not compute, the
model does, from the text. Blurring those two would make prose indistinguishable
from code, which is the entire disease this was built to treat.

WHY DIGESTION LANDS ON THE NEXT TURN, NOT THIS ONE

load_agents() snapshots the file list before it starts loading, so an agent.py
written during that sweep is picked up by the following one. That is one
message of latency and it is reported honestly rather than papered over.
"""

import ast
import base64
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import time

try:
    from agents.basic_agent import BasicAgent
except ImportError:  # standalone — no brainstem required
    class BasicAgent:
        def __init__(self, name=None, metadata=None):
            if name:
                self.name = name
            if metadata:
                self.metadata = metadata

        def perform(self, **kwargs):
            return "Not implemented."

        def to_tool(self):
            return {"type": "function", "function": {
                "name": self.name,
                "description": self.metadata.get("description", ""),
                "parameters": self.metadata.get("parameters", {})}}


__manifest__ = {
    "schema": "rapp-agent/1.0",
    "name": "@rapp/skill-digester",
    "tier": "core",
    "trust": "community",
    "version": "1.0.0",
    # Maturity ring and capability declaration, for deployments running under a
    # RAPP strain (kody-w/rapp-light). The capabilities below are not a claim —
    # a strain verifies them against this file's syntax tree and withholds the
    # agent if the code reaches anything not listed here.
    #   credential-access  os.getenv("SKILL_FEED_PATH")
    #   filesystem-write   materialising agent.py; moving refused files aside
    #   dynamic-code       compile(), to refuse a cartridge that would not parse
    "ring": "frontier",
    "capabilities": ["credential-access", "filesystem-write", "dynamic-code"],
    "tags": ["skills", "hot-load", "toaster", "portability", "singleton"],
    "example_call": {
        "args": {"action": "status"},
        "note": "What has been fed, what was digested, what was spat back out.",
    },
}

CAPSULE_RE = re.compile(r"rci-capsule:v1:([A-Za-z0-9+/=]+)")
MAX_FEED_BYTES = 4 * 1024 * 1024
MAX_PER_SWEEP = 25
# This agent writes into the folder it also reads. Without this, the first sweep
# creates digest-log.jsonl and the second sweep spits it back out as "not one of
# the three foods" — the digester eating its own excretion.
OWN_ARTIFACTS = {"digest-log.jsonl", "digest-state.json"}


def _feed_dir():
    """Beside the agents folder, not inside it: agents/ is the runtime, feed/ is
    the inbox. Mixing them would mean a half-digested file is on the load path."""
    env = os.getenv("SKILL_FEED_PATH")
    if env:
        return os.path.abspath(env)
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(here), "feed")


def _agents_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _snake(name):
    """CamelCase must split. A class named JsonDoctorAgent derived from a fed
    skill has to land on json_doctor_agent.py — the same path the original .py
    would take — or the same capability fed twice by two routes registers under
    two filenames and load_agents() quarantines one as a duplicate name."""
    s = str(name or "skill")
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)   # PRTriage -> PR_Triage
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)      # JsonDoctor -> Json_Doctor
    s = re.sub(r"[^0-9A-Za-z]+", "_", s).strip("_").lower()
    s = re.sub(r"_+", "_", s) or "skill"
    return s[:48]


def _class_name(name):
    parts = [p for p in re.split(r"[^0-9A-Za-z]+", str(name or "Skill")) if p]
    stem = "".join(p[:1].upper() + p[1:] for p in parts) or "Skill"
    if not stem.endswith("Agent"):
        stem += "Agent"
    return stem


def _frontmatter(text):
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    head, rest = text[3:end], text[end + 4:]
    out = {}
    for line in head.splitlines():
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k] = v
    return out, rest.lstrip("\n")


def _capsule(text):
    m = CAPSULE_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(gzip.decompress(base64.b64decode(m.group(1))))
    except Exception:
        return None


def _derive_params(body):
    """Real-time toasting, the honest subset: lift typed parameters out of the
    two prose shapes that actually carry them. Anything not evidenced in the
    text is not invented — an empty parameter set is a truthful answer."""
    props, required, prov = {}, [], []
    # `- \`name\` (type, required) — description`  and  `- \`name\`: description`
    for m in re.finditer(
            r"^\s*[-*]\s+`([A-Za-z_][A-Za-z0-9_]*)`\s*"
            r"(?:\(([^)]*)\))?\s*(?:[—:-]\s*)?(.*)$", body, re.M):
        key, spec, desc = m.group(1), (m.group(2) or "").lower(), m.group(3).strip()
        if key in props:
            continue
        typ = "string"
        for t in ("integer", "number", "boolean", "array", "object"):
            if t in spec or re.search(rf"\b{t}\b", desc.lower()[:40]):
                typ = "integer" if t == "integer" else t
                break
        if "int" in spec and typ == "string":
            typ = "integer"
        if "bool" in spec and typ == "string":
            typ = "boolean"
        props[key] = {"type": typ, "description": desc[:180] or key}
        if "required" in spec:
            required.append(key)
        prov.append(f"parameter {key!r} from a bulleted backtick definition")
    return props, required, prov


def _derive_steps(body):
    """Numbered procedure lines are the one prose shape that is reliably
    ordered. Bullets are not — they are used for lists of anything."""
    steps, prov = [], []
    for m in re.finditer(r"^\s*(\d+)[.)]\s+(.+?)\s*$", body, re.M):
        txt = m.group(2).strip()
        if len(txt) > 3:
            steps.append(txt[:300])
    if steps:
        prov.append(f"{len(steps)} step(s) from a numbered procedure")
    return steps, prov


SPEC_TEMPLATE = '''"""{title} — materialised from a fed SKILL.md by @rapp/skill-digester.

Fidelity tier: SPEC. This agent does not compute; it returns its own
instructions and the model acts on them. That is what the source file
contained, and claiming more would make prose indistinguishable from code.

Source: {source}
Digested: {when}
{prov_block}"""

import json

try:
    from agents.basic_agent import BasicAgent
except ImportError:
    class BasicAgent:
        def __init__(self, name=None, metadata=None):
            if name:
                self.name = name
            if metadata:
                self.metadata = metadata

        def perform(self, **kwargs):
            return "Not implemented."

        def to_tool(self):
            return {{"type": "function", "function": {{
                "name": self.name,
                "description": self.metadata.get("description", ""),
                "parameters": self.metadata.get("parameters", {{}})}}}}


__manifest__ = {manifest}

INSTRUCTIONS = {instructions!r}


class {cls}(BasicAgent):
    def __init__(self):
        self.name = {name!r}
        self.metadata = {metadata}
        super().__init__(name=self.name, metadata=self.metadata)

    def perform(self, **kwargs):
        return json.dumps({{
            "status": "ok",
            "fidelity": "SPEC",
            "instructions": INSTRUCTIONS,
            "arguments_received": kwargs,
            "note": "This capability is specified in prose. Follow the "
                    "instructions above using the arguments provided.",
        }}, indent=2)


# A materialised agent is still a single-file agent: it must run on its own,
# outside any brainstem, or the portability claim is only half true.
if __name__ == "__main__":
    import sys
    _a = sys.argv[1:]
    if _a and _a[0] == "--tool":
        print(json.dumps({cls}().to_tool(), indent=2))
    else:
        _raw = _a[0] if _a else (sys.stdin.read().strip() or "{{}}")
        print({cls}().perform(**json.loads(_raw)))


# rci-capsule:v1:{capsule}
'''


class SkillDigesterAgent(BasicAgent):
    def __init__(self):
        self.name = "SkillDigester"
        self.metadata = {
            "name": self.name,
            "description": (
                "Feed the brainstem a capability. Drop an agent.py, a toasted "
                "SKILL.md or a raw SKILL.md into the feed folder and it is "
                "digested into a live agent; anything else is spat back out "
                "with a reason. Reports what was eaten and what was refused."),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["status", "digest", "rejects", "where"],
                               "description": "status: what happened last sweep. "
                                              "digest: force a sweep now. "
                                              "rejects: what was spat back out and why. "
                                              "where: the feed folder path."},
                },
                "required": ["action"],
            },
        }
        super().__init__(name=self.name, metadata=self.metadata)
        # load_agents() runs this on EVERY /chat — it must be cheap and it must
        # never raise, or one bad fed file takes the whole brainstem down.
        try:
            self._sweep()
        except Exception as e:  # noqa: BLE001 — deliberately total
            self._last = {"error": f"{type(e).__name__}: {e}", "digested": [],
                          "rejected": [], "skipped": 0}

    # ── digestion ────────────────────────────────────────────────────────────

    def _sweep(self, force=False):
        feed = _feed_dir()
        os.makedirs(feed, exist_ok=True)
        os.makedirs(os.path.join(feed, "rejected"), exist_ok=True)
        state_path = os.path.join(feed, ".digest-state.json")
        state = {}
        if os.path.isfile(state_path):
            try:
                state = json.load(open(state_path))
            except Exception:
                state = {}

        candidates = []
        for fn in sorted(os.listdir(feed)):
            p = os.path.join(feed, fn)
            if not os.path.isfile(p) or fn.startswith(".") or fn in OWN_ARTIFACTS:
                continue
            if not fn.lower().endswith((".md", ".markdown", ".py")):
                candidates.append((p, fn, None))
                continue
            candidates.append((p, fn, "eat"))

        digested, rejected, skipped = [], [], 0
        for p, fn, kind in candidates[:MAX_PER_SWEEP]:
            try:
                sig = f"{os.path.getsize(p)}:{int(os.path.getmtime(p))}"
            except OSError:
                continue
            if not force and state.get(fn, {}).get("sig") == sig:
                skipped += 1
                continue
            if kind is None:
                rejected.append(self._spit(
                    p, fn, "not one of the three foods — expected an agent.py, "
                           "a toasted SKILL.md or a raw SKILL.md"))
                state[fn] = {"sig": sig, "verdict": "rejected"}
                continue
            try:
                res = self._digest_one(p, fn)
            except Exception as e:  # noqa: BLE001
                res = self._spit(p, fn, f"{type(e).__name__}: {e}")
            if res.get("verdict") == "digested":
                digested.append(res)
            else:
                rejected.append(res)
            state[fn] = {"sig": sig, "verdict": res.get("verdict"),
                         "detail": res.get("reason") or res.get("as")}

        try:
            json.dump(state, open(state_path, "w"), indent=2)
        except OSError:
            pass
        self._last = {"digested": digested, "rejected": rejected,
                      "skipped": skipped, "feed": feed}
        if digested:
            self._log(feed, digested + rejected)
        elif rejected:
            self._log(feed, rejected)
        return self._last

    def _log(self, feed, entries):
        try:
            with open(os.path.join(feed, "digest-log.jsonl"), "a") as fh:
                for e in entries:
                    fh.write(json.dumps(dict(e, at=int(time.time()))) + "\n")
        except OSError:
            pass

    def _spit(self, path, fn, reason):
        """Spat back out: moved to rejected/ so the next sweep does not retry it
        forever, and so the operator can see the actual file."""
        dest = os.path.join(os.path.dirname(path), "rejected", fn)
        try:
            shutil.move(path, dest)
        except OSError:
            dest = path
        return {"verdict": "spat back out", "file": fn, "reason": reason,
                "moved_to": os.path.relpath(dest, os.path.dirname(path))}

    def _digest_one(self, path, fn):
        if os.path.getsize(path) > MAX_FEED_BYTES:
            return self._spit(path, fn, f"larger than {MAX_FEED_BYTES} bytes")

        if fn.lower().endswith(".py"):
            return self._eat_agent_py(path, fn)

        text = open(path, encoding="utf-8", errors="replace").read()
        rci = _capsule(text)
        if rci:
            pres = (rci.get("preserved") or {}).get("agent")
            if pres and pres.get("b64"):
                return self._eat_toasted_exec(path, fn, rci, pres)
            return self._eat_toasted_spec(path, fn, rci, text)
        return self._eat_raw(path, fn, text)

    # 1. agent.py — already the runtime form
    def _eat_agent_py(self, path, fn, src=None, origin=None):
        src = src if src is not None else open(path, "rb").read()
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            return self._spit(path, fn, f"not parseable Python: line {e.lineno}: {e.msg}")
        classes = [n for n in ast.walk(tree)
                   if isinstance(n, ast.ClassDef) and n.name.endswith("Agent")
                   and not n.name.startswith("_")
                   and any(getattr(b, "id", getattr(b, "attr", None)) == "BasicAgent"
                           for b in n.bases)]
        if not classes:
            return self._spit(path, fn,
                              "no public class ending in 'Agent' that extends "
                              "BasicAgent — that is the singleton contract")
        stem = _snake(classes[0].name.removesuffix("Agent"))
        dest = os.path.join(_agents_dir(), f"{stem}_agent.py")
        if os.path.exists(dest) and open(dest, "rb").read() == src:
            return {"verdict": "digested", "file": fn, "as": os.path.basename(dest),
                    "fidelity": "EXEC", "note": "already present, unchanged",
                    "class": classes[0].name}
        with open(dest, "wb") as fh:
            fh.write(src)
        return {"verdict": "digested", "file": fn, "as": os.path.basename(dest),
                "fidelity": "EXEC", "class": classes[0].name,
                "route": origin or "agent.py taken as-is",
                "live": "on your next message"}

    # 2. toasted SKILL.md carrying a real agent payload
    def _eat_toasted_exec(self, path, fn, rci, pres):
        try:
            src = gzip.decompress(base64.b64decode(pres["b64"]))
        except Exception as e:
            return self._spit(path, fn, f"unreadable rci capsule payload: {e}")
        want = pres.get("sha256")
        got = hashlib.sha256(src).hexdigest()
        if want and got != want:
            return self._spit(path, fn,
                              "capsule payload failed its checksum — the skill "
                              f"was altered after toasting (want {want[:12]}, "
                              f"got {got[:12]})")
        return self._eat_agent_py(path, fn, src=src,
                                  origin="toasted SKILL.md → agent.py "
                                         "(capsule payload, checksum verified)")

    # 3. toasted SKILL.md that is specification only
    def _eat_toasted_spec(self, path, fn, rci, text):
        return self._materialise_spec(
            path, fn, rci,
            instructions=(rci.get("instructions")
                          or _frontmatter(text)[1])[:20000],
            provenance=(rci.get("provenance") or [])[:12],
            route="toasted SKILL.md → agent.py (specification only)")

    # 4. raw SKILL.md — toasted in real time, then materialised
    def _eat_raw(self, path, fn, text):
        fm, body = _frontmatter(text)
        name = fm.get("name") or os.path.splitext(fn)[0]
        desc = fm.get("description") or ""
        if not desc:
            for line in body.splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    desc = s[:300]
                    break
        if not desc:
            return self._spit(path, fn,
                              "no description in frontmatter and no prose to "
                              "take one from — nothing here to digest")
        props, required, p1 = _derive_params(body)
        steps, p2 = _derive_steps(body)
        rci = {"name": name, "slug": _snake(name), "description": desc,
               "instructions": body[:20000],
               "parameters": {"type": "object", "properties": props,
                              "required": required},
               "impl": {"steps": steps},
               "provenance": p1 + p2}
        return self._materialise_spec(
            path, fn, rci, instructions=body[:20000],
            provenance=(p1 + p2)[:12],
            route="raw SKILL.md → toasted in real time → agent.py")

    def _materialise_spec(self, path, fn, rci, instructions, provenance, route):
        name = rci.get("name") or os.path.splitext(fn)[0]
        cls = _class_name(name)
        stem = _snake(rci.get("slug") or name)
        tool_name = re.sub(r"[^0-9A-Za-z_-]", "_", str(name))[:60] or stem
        params = rci.get("parameters") or {"type": "object", "properties": {}}
        if not isinstance(params.get("properties"), dict):
            params = {"type": "object", "properties": {}}
        metadata = {
            "name": tool_name,
            "description": (rci.get("description") or name)[:900],
            "parameters": params,
        }
        manifest = {
            "schema": "rapp-agent/1.0",
            "name": f"@fed/{stem}",
            "tier": "community",
            "trust": "unverified",
            "version": "1.0.0",
            "tags": ["fed", "spec", "singleton"],
            "example_call": {"args": {}, "note": "Specified in prose; SPEC tier."},
        }
        prov_block = ""
        if provenance:
            prov_block = "\nDerived deterministically from the prose:\n" + \
                "\n".join(f"  - {p}" for p in provenance) + "\n"
        try:
            capsule = base64.b64encode(
                gzip.compress(json.dumps(rci, sort_keys=True).encode())).decode()
        except Exception:
            capsule = ""
        src = SPEC_TEMPLATE.format(
            title=name, source=fn,
            when=time.strftime("%Y-%m-%d %H:%M:%S"),
            prov_block=prov_block,
            manifest=json.dumps(manifest, indent=4),
            instructions=instructions,
            cls=cls, name=tool_name,
            metadata=json.dumps(metadata, indent=12).replace("\n}", "\n        }"),
            capsule=capsule,
        ).encode()
        try:
            compile(src, "<materialised>", "exec")
        except SyntaxError as e:
            return self._spit(path, fn,
                              "materialised agent did not compile "
                              f"(line {e.lineno}: {e.msg}) — refusing to write a "
                              "broken cartridge into agents/")
        dest = os.path.join(_agents_dir(), f"{stem}_agent.py")
        with open(dest, "wb") as fh:
            fh.write(src)
        return {"verdict": "digested", "file": fn,
                "as": os.path.basename(dest), "fidelity": "SPEC",
                "class": cls, "route": route,
                "typed_parameters": sorted((params.get("properties") or {})),
                "derivation": provenance,
                "live": "on your next message"}

    # ── the wire ─────────────────────────────────────────────────────────────

    def perform(self, **kwargs):
        action = kwargs.get("action") or "status"
        feed = _feed_dir()
        try:
            if action == "where":
                return json.dumps({
                    "status": "ok", "feed_folder": feed,
                    "rejected_folder": os.path.join(feed, "rejected"),
                    "accepts": ["agent.py", "toasted SKILL.md", "raw SKILL.md"],
                    "note": "drop a file in; it is digested on the next message",
                }, indent=2)

            if action == "digest":
                last = self._sweep(force=True)
            else:
                last = getattr(self, "_last", None) or {"digested": [],
                                                        "rejected": [], "skipped": 0}

            if action == "rejects":
                rejdir = os.path.join(feed, "rejected")
                files = sorted(os.listdir(rejdir)) if os.path.isdir(rejdir) else []
                return json.dumps({
                    "status": "ok", "spat_back_out": len(files),
                    "files": files[:50],
                    "this_sweep": last.get("rejected", []),
                    "note": "a file lands here when it is not one of the three "
                            "foods, or when it is but is malformed",
                }, indent=2)

            d, r = last.get("digested", []), last.get("rejected", [])
            if d and not r:
                headline = f"it digested {len(d)}"
            elif r and not d:
                headline = f"it spat {len(r)} back out"
            elif d or r:
                headline = f"it digested {len(d)}, spat {len(r)} back out"
            else:
                headline = "nothing new in the feed"
            return json.dumps({
                "status": "ok", "headline": headline,
                "feed_folder": feed,
                "digested": d, "spat_back_out": r,
                "unchanged_since_last_sweep": last.get("skipped", 0),
                "error": last.get("error"),
                "note": "a digested capability is live on your next message — "
                        "load_agents() snapshots the file list before it loads",
            }, indent=2)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"status": "error",
                               "message": f"{type(e).__name__}: {e}"}, indent=2)


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "--tool":
        print(json.dumps(SkillDigesterAgent().to_tool(), indent=2))
    else:
        raw = a[0] if a else (sys.stdin.read().strip() or '{"action":"status"}')
        print(SkillDigesterAgent().perform(**json.loads(raw)))
