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
import pprint
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
MODE = {mode!r}
# Tried in order. Every entry must serve the SAME bytes — the digest above is
# what makes a mirror a mirror rather than a second source of truth.
SOURCES = {sources}
LANDING = os.path.expanduser(os.getenv("RAPP_EGG_LANDING", {landing!r}))

EGG_B64 = (
{payload}
)


def _verify(raw, origin):
    got = hashlib.sha256(raw).hexdigest()
    if got != EGG_SHA256:
        raise ValueError(
            f"egg from {{origin}} failed its checksum: expected "
            f"{{EGG_SHA256[:16]}}, got {{got[:16]}} — refusing to hand on bytes "
            f"that are not the ones this cartridge was built for")
    return raw


def _cached():
    """A landed egg that still matches the digest is the payload. This is what
    makes a referenced cartridge fetch exactly once, ever."""
    dest = os.path.join(LANDING, EGG_FILENAME)
    if not os.path.isfile(dest):
        return None
    try:
        with open(dest, "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    return raw if hashlib.sha256(raw).hexdigest() == EGG_SHA256 else None


def egg_bytes(timeout=15):
    """The payload, verified, however it arrives.

    Embedded cartridges carry it. Referenced cartridges fetch it once from a
    public source and cache it. Either way the SHA-256 is pinned at pack time,
    so a reference is not a trust relationship with a URL — if the source ever
    serves different bytes, this refuses them."""
    if EGG_B64:
        return _verify(base64.b64decode("".join(EGG_B64.split())), "the cartridge")
    hit = _cached()
    if hit is not None:
        return hit
    if not SOURCES:
        raise ValueError("this cartridge references an egg but lists no source")
    import urllib.request
    errors, tampered = [], []
    for url in SOURCES:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                raw = r.read()
        except Exception as e:  # noqa: BLE001
            errors.append(f"{{url}} -> {{type(e).__name__}}: {{e}}")
            continue
        try:
            return _verify(raw, url)
        except ValueError as e:
            # Reached the source and it served the WRONG BYTES. That is a
            # different fact from "the network is down", and burying it in a
            # list of transport errors is how a substituted payload goes
            # unnoticed. Report it first and on its own.
            tampered.append(str(e))
    if tampered:
        raise ValueError(
            "REFUSED: a source served bytes that do not match the digest this "
            "cartridge pins. " + " | ".join(tampered))
    raise ValueError(
        "could not fetch the referenced egg from any source. "
        + " | ".join(errors)
        + ". An embedded cartridge (cartridge pack) needs no network.")


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
        if _cached() is not None:
            return dest, False                  # already here, byte-identical
        raw = egg_bytes()
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


EGG_HUB_INDEX = ("https://raw.githubusercontent.com/kody-w/"
                 "rapp-egg-hub/main/index.json")


def hub_lookup(slug, index_url=None):
    """Resolve a slug against the published egg hub.

    The hub already publishes sha256, raw_url, egg_schema and size_bytes per
    entry (`rapp-egg-hub/2.0`), which is everything a referenced cartridge
    needs. Nothing here invents a registry — Article XLVII forbids that, and
    the hub exists."""
    import urllib.request
    with urllib.request.urlopen(index_url or EGG_HUB_INDEX, timeout=20) as r:
        idx = json.loads(r.read())
    base = (idx.get("raw_base") or "").rstrip("/")
    for e in idx.get("eggs") or []:
        if e.get("slug") == slug or e.get("name") == slug:
            url = e.get("raw_url") or (
                f"{base}/{str(e.get('egg_path') or '').lstrip('/')}" if base else None)
            return {"slug": e.get("slug"), "url": url,
                    "sha256": e.get("sha256"), "size": e.get("size_bytes"),
                    "schema": e.get("egg_schema"), "kind": e.get("kind"),
                    "name": e.get("display_name") or e.get("name")}
    return None


def emit(out, *, stem, egg_name, meta, sha, size, mode, payload, sources,
         agent_name=None):
    manifest = {
        "schema": "rapp-agent/1.0",
        "name": f"@cartridge/{stem}",
        "kind": "agent",
        "version": "1.0.0",
        "summary": f"Self-bootstrapping cartridge carrying {egg_name}.",
        "tags": ["cartridge", "egg", "portable", "singleton", mode],
        "ring": "frontier",
        "capabilities": (["credential-access", "filesystem-write", "dynamic-code"]
                         + (["network"] if mode == "ref" else [])),
        "carries": {"egg": egg_name, "schema": meta.get("schema"),
                    "kind": meta.get("kind"), "sha256": sha, "mode": mode},
    }
    agent_name = agent_name or re.sub(r"[^0-9A-Za-z_-]", "_", f"{stem}_cartridge")[:60]
    text = TEMPLATE.format(
        title=(meta.get("name") or stem).replace("_", " ").title(),
        egg_name=egg_name, container=meta.get("container") or "referenced",
        schema=meta.get("schema"), kind=meta.get("kind"),
        kind_desc=meta.get("kind") or "unknown-kind",
        size=size or 0, sha256=sha,
        manifest=pprint.pformat(manifest, indent=4, width=74,
                                sort_dicts=False),
        payload=payload, landing=LANDING_DEFAULT, mode=mode,
        sources=pprint.pformat(sources, indent=4, width=74),
        cls=_class(stem) + "CartridgeAgent", agent_name=agent_name,
    )
    # Never emit a cartridge that will not load. compile() alone is not enough:
    # it catches SyntaxError, but a bad literal (a JSON `null` in Python source)
    # is a NameError that only appears when the module is EXECUTED — which is
    # precisely what the receiving brainstem does on import. So execute it here,
    # in a throwaway namespace. Nothing runs at module scope except constants
    # and a class definition; the agent's __init__ is not called.
    try:
        compile(text, out, "exec")
    except SyntaxError as e:
        sys.exit(f"cartridge: generated file does not compile "
                 f"(line {e.lineno}: {e.msg}) — refusing to write it")
    try:
        exec(compile(text, out, "exec"), {"__name__": "_cartridge_probe"})
    except Exception as e:  # noqa: BLE001
        sys.exit(f"cartridge: generated file does not import "
                 f"({type(e).__name__}: {e}) — refusing to write a cartridge "
                 f"the receiving brainstem would roll back")
    with open(out, "w") as fh:
        fh.write(text)
    return out


def cmd_ref(args):
    """A cartridge that references its egg from a public source.

    Same carrier, same refusal discipline, same delegation to the hatcher — the
    only difference is where the bytes come from. The digest is pinned at pack
    time, so a reference is not a trust relationship with a URL: if the source
    ever serves different bytes, the cartridge refuses them."""
    target = args.target
    if target.startswith(("http://", "https://")):
        url, sha, size = target, args.sha256, None
        if not sha:
            if not args.allow_unpinned:
                sys.exit("cartridge: refusing to build an unpinned reference. "
                         "Pass --sha256, or point at a hub slug which publishes "
                         "one. Without a digest the cartridge would hand on "
                         "whatever that URL serves later.")
            sys.exit("cartridge: --allow-unpinned is deliberately not "
                     "implemented; pin the digest.")
        meta = {"schema": args.schema, "kind": args.kind,
                "name": args.name, "container": "referenced"}
        egg_name = args.egg_name or os.path.basename(url.split("?")[0])
        stem = _snake(args.name or re.sub(r"\.egg$", "", egg_name))
        sources = [url] + list(args.mirror or [])
    else:
        hit = hub_lookup(target, args.index)
        if not hit:
            sys.exit(f"cartridge: {target!r} is not in the egg hub index. "
                     f"Pass a full URL with --sha256 instead.")
        if not hit.get("url") or not hit.get("sha256"):
            sys.exit(f"cartridge: hub entry {target!r} has no raw_url or sha256 "
                     f"— cannot build a verifiable reference from it.")
        url, sha, size = hit["url"], hit["sha256"], hit.get("size")
        meta = {"schema": hit.get("schema"), "kind": hit.get("kind"),
                "name": hit.get("name"), "container": "referenced"}
        egg_name = os.path.basename(url.split("?")[0])
        stem = _snake(hit.get("slug") or egg_name)
        sources = [url] + list(args.mirror or [])

    out = args.out or f"{stem}_cartridge_agent.py"
    emit(out, stem=stem, egg_name=egg_name, meta=meta, sha=sha, size=size or 0,
         mode="ref", payload='    ""', sources=sources)
    print(f"  referenced {egg_name} -> {out}")
    print(f"    schema      {meta.get('schema')}")
    print(f"    kind        {meta.get('kind')}")
    print(f"    egg         {(size or 0):,} bytes   sha {sha[:16]}")
    print(f"    sources     {len(sources)}")
    for s in sources:
        print(f"      - {s}")
    print(f"    cartridge   {os.path.getsize(out):,} bytes")
    print("\n  Fetches once, verifies against the pinned digest, then caches.")
    return 0


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

    out = args.out or f"{stem}_cartridge_agent.py"
    emit(out, stem=stem, egg_name=egg_name, meta=meta, sha=sha, size=len(raw),
         mode="embed", payload=payload, sources=[])
    print(f"  packed {egg_name} -> {out}")
    print(f"    container   {meta['container']}")
    print(f"    schema      {meta['schema']}")
    print(f"    kind        {meta['kind']}")
    print(f"    egg         {len(raw):,} bytes   sha {sha[:16]}")
    print(f"    cartridge   {os.path.getsize(out):,} bytes "
          f"({os.path.getsize(out)/max(1,len(raw)):.2f}x)")
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
    q = sub.add_parser("ref", help="reference an egg from a public source")
    q.add_argument("target", help="an egg-hub slug, or a full https URL")
    q.add_argument("-o", "--out")
    q.add_argument("--sha256", help="required when target is a URL")
    q.add_argument("--mirror", action="append", help="additional source, same bytes")
    q.add_argument("--index", help="alternate hub index URL")
    q.add_argument("--schema"); q.add_argument("--kind"); q.add_argument("--name")
    q.add_argument("--egg-name")
    q.add_argument("--allow-unpinned", action="store_true",
                   help=argparse.SUPPRESS)
    q.set_defaults(fn=cmd_ref)

    q = sub.add_parser("inspect", help="what is this cartridge carrying?")
    q.add_argument("cartridge")
    q.set_defaults(fn=cmd_inspect)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
