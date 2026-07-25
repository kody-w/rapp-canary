#!/usr/bin/env python3
"""cartridge — wrap a .egg inside a single self-bootstrapping agent.py.

    cartridge pack <thing.egg> [-o out_agent.py] [--name NAME]
    cartridge inspect <cartridge_agent.py>

WHY THIS EXISTS

Article L is right that `.egg` is the only portable container. It is also true
that nobody who receives a `.egg` on a phone knows what to do with it. There is
no app for it, AirDrop hands it to Files, and it sits there.

An `agent.py` has the opposite property: it is the one thing the brainstem's
import wire already accepts, `/agents/import` loads and validates it
synchronously, and it is one file. So the cartridge keeps the egg as the
container and gives it a carrier that the receiving end already understands.

The egg is not replaced or re-formatted. It is embedded verbatim, with its
SHA-256, and written back out byte-identical on arrival.

WHY THE CARTRIDGE DOES NOT HATCH BY KIND

Article L.3: the universal hatcher "is the only thing that decides where a
cartridge hatches", and it MUST refuse unknown kinds rather than guess. Kinds in
the wild already exceed the five in the article — `brainstem-egg/2.3-cubby` and
`2.3-neighborhood` both exist — so a carrier that dispatched by kind would be a
second, competing hatcher that goes stale the moment a sixth kind ships.

So the cartridge does exactly three things: verify the bytes, write the egg
where the hatcher expects to find it, and hand it over. If the universal hatcher
is not installed it says so and stops, leaving the egg on disk. It never
guesses, and it never writes outside the landing directory.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import zipfile

LANDING_DEFAULT = "~/.brainstem-eggs"


def _snake(s):
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(s or "cartridge"))
    s = re.sub(r"[^0-9A-Za-z]+", "_", s).strip("_").lower()
    return (re.sub(r"_+", "_", s) or "cartridge")[:40]


def _class(s):
    parts = [p for p in re.split(r"[^0-9A-Za-z]+", str(s)) if p]
    return "".join(p[:1].upper() + p[1:] for p in parts) or "Cartridge"


def read_egg_metadata(path):
    """What kind of cartridge is this? Read it, never assume it.

    Two container shapes exist: a ZIP with a manifest inside (2.x) and a legacy
    JSON envelope. Both must stay readable forever (Article L.4), so both are
    probed and neither is rewritten."""
    with open(path, "rb") as fh:
        head = fh.read(4)
    meta = {"container": None, "schema": None, "kind": None, "name": None}
    if head[:2] == b"PK":
        meta["container"] = "zip"
        try:
            with zipfile.ZipFile(path) as z:
                for n in z.namelist():
                    if os.path.basename(n).lower() == "manifest.json":
                        m = json.loads(z.read(n))
                        meta["schema"] = m.get("schema")
                        meta["kind"] = m.get("type") or m.get("kind")
                        meta["name"] = m.get("name") or m.get("slug")
                        break
                else:
                    # Some kinds carry the schema in a sibling card instead of a
                    # manifest. Record honestly that we could not read one.
                    meta["schema"] = None
        except zipfile.BadZipFile:
            meta["container"] = "unknown"
    else:
        try:
            with open(path) as fh:
                d = json.load(fh)
            meta["container"] = "json"
            meta["schema"] = d.get("schema") or (
                f"legacy-egg/{d.get('_schema_version')}" if d.get("_format") == "egg" else None)
            org = d.get("organism") or {}
            meta["kind"] = d.get("type") or ("organism" if org else None)
            meta["name"] = org.get("slug") or d.get("name")
        except Exception:
            meta["container"] = "unknown"
    if meta["schema"] and not meta["kind"]:
        # brainstem-egg/2.3-cubby -> cubby
        tail = str(meta["schema"]).rsplit("-", 1)
        if len(tail) == 2:
            meta["kind"] = tail[1]
    return meta


TEMPLATE = '''"""{title} — a self-bootstrapping RAPP cartridge.

This is one file. It is a real agent, so the brainstem's ordinary import path
loads and validates it like any other. Embedded inside it is a `.egg`
cartridge, verbatim and verifiable:

    egg          {egg_name}
    container    {container}
    schema       {schema}
    kind         {kind}
    size         {size:,} bytes
    sha256       {sha256}

WHY THE EGG TRAVELS INSIDE AN agent.py

Article L is right that the `.egg` is the only portable container. It is also
true that nobody who receives a `.egg` on a phone knows what to do with it —
AirDrop hands it to Files and it sits there. An `agent.py` is the one thing the
receiving brainstem already accepts: `/agents/import` loads it, validates it,
and rolls back if it does not work. So the egg keeps being the container, and
this is the envelope that gets it through the door.

The egg is embedded byte-for-byte. Nothing is re-formatted, and the SHA-256
above is checked before anything is written.

WHAT THIS DOES ON ARRIVAL

`load_agents()` runs on every /chat, so `__init__` is a per-turn hook. On the
first turn after arrival this writes the egg into the landing directory and
verifies its digest. Then it hands the cartridge to the universal hatcher.

WHAT IT DELIBERATELY DOES NOT DO

It does not decide where the cartridge hatches. Article L.3 reserves that for
the universal hatcher, which must refuse unknown kinds rather than guess — and
kinds already exceed the five named in the article. A carrier that dispatched
by kind would be a second hatcher that goes stale on the next kind that ships.

So if the universal hatcher is not installed, this stops and says so, leaving a
verified egg on disk. Refusing is the specified behaviour, not a limitation.
"""

import base64
import hashlib
import json
import os
import sys

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
            return {{"type": "function", "function": {{
                "name": self.name,
                "description": self.metadata.get("description", ""),
                "parameters": self.metadata.get("parameters", {{}})}}}}


__manifest__ = {manifest}

EGG_FILENAME = {egg_name!r}
EGG_SCHEMA = {schema!r}
EGG_KIND = {kind!r}
EGG_SHA256 = {sha256!r}
EGG_BYTES = {size}
LANDING = os.path.expanduser(os.getenv("RAPP_EGG_LANDING", {landing!r}))

EGG_B64 = (
{payload}
)


def egg_bytes():
    """Decode and verify. A cartridge that hands over unverified bytes is worse
    than one that fails, because the failure surfaces later and somewhere else."""
    raw = base64.b64decode("".join(EGG_B64.split()))
    got = hashlib.sha256(raw).hexdigest()
    if got != EGG_SHA256:
        raise ValueError(
            f"embedded egg failed its checksum: expected {{EGG_SHA256[:16]}}, "
            f"got {{got[:16]}} — this cartridge was altered in transit")
    return raw


def _marker_path():
    return os.path.join(LANDING, f".{{EGG_FILENAME}}.hatched")


class {cls}(BasicAgent):
    def __init__(self):
        self.name = {agent_name!r}
        self.metadata = {{
            "name": self.name,
            "description": (
                "A self-bootstrapping cartridge carrying the {kind_desc} egg "
                "{egg_name!r}. Reports what it is carrying, writes the verified "
                "egg to disk, and hands it to the universal hatcher."),
            "parameters": {{
                "type": "object",
                "properties": {{
                    "action": {{"type": "string",
                               "enum": ["status", "save", "hatch", "verify"],
                               "description": "status: what this is carrying and "
                                              "whether it landed. save: write the "
                                              "egg out without hatching. hatch: "
                                              "hand it to the universal hatcher. "
                                              "verify: check the embedded digest."}},
                }},
                "required": ["action"],
            }},
        }}
        super().__init__(name=self.name, metadata=self.metadata)
        # Runs on every /chat. Must be cheap and must never raise: an exception
        # here would take down the brainstem this cartridge is trying to join.
        self._landing = None
        self._hatch = None
        try:
            if not os.path.exists(_marker_path()):
                self._bootstrap()
        except Exception as e:  # noqa: BLE001 — deliberately total
            self._hatch = {{"status": "error", "detail": f"{{type(e).__name__}}: {{e}}"}}

    # ---- arrival ----

    def _save(self):
        os.makedirs(LANDING, exist_ok=True)
        dest = os.path.join(LANDING, EGG_FILENAME)
        raw = egg_bytes()
        if os.path.exists(dest):
            with open(dest, "rb") as fh:
                if hashlib.sha256(fh.read()).hexdigest() == EGG_SHA256:
                    return dest, False          # already here, byte-identical
        tmp = dest + ".part"
        with open(tmp, "wb") as fh:
            fh.write(raw)
        os.replace(tmp, dest)
        return dest, True

    def _find_hatcher(self):
        """The universal hatcher is an agent, so look for it the way the
        brainstem does — as a file in agents/ — rather than importing a package
        that may not exist."""
        here = os.path.dirname(os.path.abspath(__file__))
        cand = os.path.join(here, "egg_hatcher_agent.py")
        if not os.path.isfile(cand):
            return None
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("_egg_hatcher", cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:  # noqa: BLE001
            return {{"error": f"hatcher present but would not load: {{e}}"}}
        for attr in dir(mod):
            obj = getattr(mod, attr)
            if (isinstance(obj, type) and attr.endswith("Agent")
                    and attr != "BasicAgent"):
                return {{"module": mod, "cls": obj, "path": cand}}
        return {{"error": "egg_hatcher_agent.py has no agent class"}}

    def _bootstrap(self):
        dest, wrote = self._save()
        self._landing = {{"path": dest, "written": wrote}}
        found = self._find_hatcher()
        if found is None:
            self._hatch = {{
                "status": "refused",
                "reason": "the universal hatcher is not installed on this "
                          "brainstem, and this cartridge does not decide where "
                          "an egg hatches (Article L.3 — it must refuse unknown "
                          "kinds, not guess)",
                "egg_is_at": dest,
                "next_step": "install egg_hatcher_agent.py, then run this agent "
                             "with action='hatch'",
            }}
            return
        if "error" in found:
            self._hatch = {{"status": "error", "detail": found["error"],
                          "egg_is_at": dest}}
            return
        try:
            agent = found["cls"]()
            # Call the hatcher by its OWN declared schema rather than a guessed
            # signature. The shipped hatcher takes `egg_path`; earlier drafts of
            # this carrier assumed `path`, which fails silently at the last step
            # of an otherwise working chain. Reading the metadata makes the
            # carrier survive the hatcher changing its parameter name.
            props = {{}}
            try:
                props = (getattr(agent, "metadata", {{}}) or {{}}).get(
                    "parameters", {{}}).get("properties", {{}}) or {{}}
            except Exception:  # noqa: BLE001
                props = {{}}
            key = next((k for k in ("egg_path", "path", "egg", "cartridge", "file")
                        if k in props), None)
            if key is None:
                self._hatch = {{
                    "status": "refused",
                    "reason": "the installed hatcher declares no parameter this "
                              "carrier recognises, so there is no safe way to "
                              "hand it the cartridge",
                    "hatcher_parameters": sorted(props),
                    "egg_is_at": dest,
                }}
                return
            out = agent.perform(**{{key: dest}})
            self._hatch = {{"status": "handed_to_hatcher",
                          "hatcher": os.path.basename(found["path"]),
                          "result": out[:800] if isinstance(out, str) else out}}
            with open(_marker_path(), "w") as fh:
                json.dump({{"egg": EGG_FILENAME, "sha256": EGG_SHA256}}, fh)
        except Exception as e:  # noqa: BLE001
            self._hatch = {{"status": "error", "egg_is_at": dest,
                          "detail": f"the hatcher raised: {{type(e).__name__}}: {{e}}"}}

    # ---- the wire ----

    def perform(self, **kwargs):
        action = kwargs.get("action") or "status"
        try:
            if action == "verify":
                egg_bytes()
                return json.dumps({{"status": "ok", "verified": True,
                                  "sha256": EGG_SHA256, "bytes": EGG_BYTES}}, indent=2)
            if action == "save":
                dest, wrote = self._save()
                return json.dumps({{"status": "ok", "egg_is_at": dest,
                                  "written": wrote,
                                  "note": "verified against the embedded digest"}},
                                 indent=2)
            if action == "hatch":
                self._bootstrap()
                return json.dumps({{"status": "ok", "landing": self._landing,
                                  "hatch": self._hatch}}, indent=2)
            return json.dumps({{
                "status": "ok",
                "carrying": {{"egg": EGG_FILENAME, "schema": EGG_SCHEMA,
                             "kind": EGG_KIND, "bytes": EGG_BYTES,
                             "sha256": EGG_SHA256[:16]}},
                "landing": self._landing,
                "hatch": self._hatch,
                "already_hatched": os.path.exists(_marker_path()),
            }}, indent=2)
        except Exception as e:  # noqa: BLE001
            return json.dumps({{"status": "error",
                              "message": f"{{type(e).__name__}}: {{e}}"}}, indent=2)


if __name__ == "__main__":
    _a = sys.argv[1:]
    if _a and _a[0] == "--tool":
        print(json.dumps({cls}().to_tool(), indent=2))
    else:
        _raw = _a[0] if _a else (sys.stdin.read().strip() or '{{"action":"status"}}')
        print({cls}().perform(**json.loads(_raw)))
'''


def cmd_pack(args):
    src = os.path.abspath(args.egg)
    if not os.path.isfile(src):
        sys.exit(f"cartridge: no such egg: {src}")
    with open(src, "rb") as fh:
        raw = fh.read()
    meta = read_egg_metadata(src)
    if meta["container"] == "unknown":
        sys.exit("cartridge: this file is neither a ZIP nor a JSON egg envelope "
                 "— refusing to wrap something that is not a cartridge")

    egg_name = os.path.basename(src)
    stem = _snake(args.name or meta.get("name") or
                  re.sub(r"\.egg$", "", egg_name))
    sha = hashlib.sha256(raw).hexdigest()

    b64 = base64.b64encode(raw).decode()
    # Wrap so the generated file is readable and diffable rather than one
    # 200-kilobyte line that every editor chokes on.
    lines = [b64[i:i + 76] for i in range(0, len(b64), 76)]
    payload = "\n".join(f'    "{ln}"' for ln in lines)

    manifest = {
        "schema": "rapp-agent/1.0",
        "name": f"@cartridge/{stem}",
        "kind": "agent",
        "version": "1.0.0",
        "summary": f"Self-bootstrapping cartridge carrying {egg_name}.",
        "tags": ["cartridge", "egg", "portable", "singleton"],
        "ring": "frontier",
        "capabilities": ["credential-access", "filesystem-write", "dynamic-code"],
        "carries": {"egg": egg_name, "schema": meta["schema"],
                    "kind": meta["kind"], "sha256": sha},
    }
    agent_name = re.sub(r"[^0-9A-Za-z_-]", "_", f"{stem}_cartridge")[:60]
    out = args.out or f"{stem}_cartridge_agent.py"
    text = TEMPLATE.format(
        title=(meta.get("name") or stem).replace("_", " ").title(),
        egg_name=egg_name, container=meta["container"],
        schema=meta["schema"], kind=meta["kind"],
        kind_desc=meta["kind"] or "unknown-kind",
        size=len(raw), sha256=sha,
        manifest=json.dumps(manifest, indent=4),
        payload=payload, landing=LANDING_DEFAULT,
        cls=_class(stem) + "CartridgeAgent", agent_name=agent_name,
    )
    # Never emit a cartridge that will not load — the receiving brainstem would
    # roll it back and the operator would be left guessing which end broke.
    try:
        compile(text, out, "exec")
    except SyntaxError as e:
        sys.exit(f"cartridge: generated file does not compile "
                 f"(line {e.lineno}: {e.msg}) — refusing to write it")
    with open(out, "w") as fh:
        fh.write(text)

    print(f"  packed {egg_name} -> {out}")
    print(f"    container   {meta['container']}")
    print(f"    schema      {meta['schema']}")
    print(f"    kind        {meta['kind']}")
    print(f"    egg         {len(raw):,} bytes   sha {sha[:16]}")
    print(f"    cartridge   {os.path.getsize(out):,} bytes "
          f"({os.path.getsize(out)/max(1,len(raw)):.2f}x)")
    print(f"    agent       {agent_name}")
    print("\n  One file. AirDrop it, or import it at Agents -> Receive an agent.")
    return 0


def cmd_inspect(args):
    path = os.path.abspath(args.cartridge)
    import ast
    with open(path, "rb") as fh:
        tree = ast.parse(fh.read())
    got = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            t = node.targets[0]
            if isinstance(t, ast.Name) and t.id in (
                    "EGG_FILENAME", "EGG_SCHEMA", "EGG_KIND", "EGG_SHA256",
                    "EGG_BYTES", "__manifest__"):
                try:
                    got[t.id] = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    pass
    if "EGG_FILENAME" not in got:
        sys.exit("cartridge: this file is not a cartridge")
    print(json.dumps({k: v for k, v in got.items() if k != "__manifest__"},
                     indent=2))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="cartridge",
                                description="Wrap a .egg in a self-bootstrapping agent.py.")
    sub = p.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("pack", help="wrap an egg")
    q.add_argument("egg")
    q.add_argument("-o", "--out")
    q.add_argument("--name")
    q.set_defaults(fn=cmd_pack)
    q = sub.add_parser("inspect", help="what is this cartridge carrying?")
    q.add_argument("cartridge")
    q.set_defaults(fn=cmd_inspect)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
