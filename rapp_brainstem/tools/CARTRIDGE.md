# Cartridges — an `.egg` that travels inside an `agent.py`

Every `.egg` travels the same way: as one `agent.py` that either **carries**
the egg or **references** it from a public source. Same carrier, same refusal
discipline, same delegation — the only difference is where the bytes come from.

```bash
# embedded — carries the egg, works offline and across an air gap
python3 rapp_brainstem/tools/cartridge.py pack thing.egg

# referenced — fetches once from the published egg hub, digest pinned
python3 rapp_brainstem/tools/cartridge.py ref grandma-rose

# referenced from any URL (a digest is mandatory)
python3 rapp_brainstem/tools/cartridge.py ref https://…/x.egg --sha256 <hex> \
        --mirror https://…/mirror/x.egg
```

|              | embedded | referenced |
|---|---|---|
| carrier size | egg × 1.33 + ~12 KB | ~13 KB, any egg |
| network      | never | once, then cached |
| air gap      | yes | no |
| digest       | pinned | pinned |

`ref <slug>` resolves against `kody-w/rapp-egg-hub`, whose index already
publishes `sha256`, `raw_url`, `egg_schema` and `size_bytes` per entry
(`rapp-egg-hub/2.0`). Nothing here invents a registry — Article XLVII forbids
that, and the hub exists.

AirDrop that file, or take it in at **Agents → Receive an agent**. On the next
message the brainstem has it.

## Why

Article L is right that the `.egg` is the only portable container. It is also
true that **nobody who receives a `.egg` on a phone knows what to do with it** —
AirDrop hands it to Files and it sits there. There is no app for it.

An `agent.py` has the opposite property: it is the one thing the receiving
brainstem already accepts. `/agents/import` writes it, loads it, validates it,
and rolls back if it does not work — a complete hot-load path that already
exists (RAPP/1 §8.2, "auto-discovered every request").

So the egg keeps being the container. The cartridge is the envelope that gets it
through a door that is already open.

## What it does not do

It does not decide where the cartridge hatches. **Article L.3** reserves that
for the universal hatcher, which must refuse unknown kinds rather than guess.
Kinds in the wild already exceed the five named in the article —
`brainstem-egg/2.3-cubby` and `2.3-neighborhood` both exist — so a carrier that
dispatched by kind would be a second, competing hatcher that goes stale on the
next kind that ships.

The cartridge therefore does exactly three things:

1. **verify** — SHA-256 checked before anything is written, in both modes
2. **land** — write the egg byte-identical into `~/.brainstem-eggs`
   (`RAPP_EGG_LANDING` to override)
3. **hand over** — call the universal hatcher, using the hatcher's *own declared
   parameter name* rather than a guessed signature

If `egg_hatcher_agent.py` is not installed it stops and says so, leaving a
verified egg on disk. Refusing is the specified behaviour, not a limitation.

## Properties

- **Both container shapes.** ZIP (`brainstem-egg/2.x`) and the legacy JSON
  envelope both pack; neither is rewritten (Article L.4 — old schemas never die).
- **Idempotent.** A second load re-verifies and does not rewrite.
- **Tamper-evident.** One flipped byte in the payload fails the digest with a
  message naming the expected and actual hashes.
- **Offline.** Every hatcher shipped to date fetches its egg over the network.
  This one carries it, so a cartridge works on a plane and across an air gap.
- **Never emits a broken cartridge.** The packer compiles the generated file and
  refuses to write it if it would not load — otherwise the receiving brainstem
  rolls it back and the operator is left guessing which end broke.

## Cost

Base64 is 1.33×, plus a fixed ~12 KB of carrier. A 27 KB egg becomes a 51 KB
agent; a 167 KB egg becomes about 235 KB. That is the price of one file the
receiver already understands.

## A reference is not trust in a URL

The digest is pinned at pack time. If a source later serves different bytes the
cartridge **refuses them**, and says so in those words rather than burying it in
a list of transport errors — "the network is down" and "the source substituted
the payload" are different facts, and only one of them is an attack.

An unpinned reference cannot be built at all: `ref` exits rather than emit a
cartridge that would hand on whatever a URL serves next month.

## Two honest edges

- **Name collisions are the kernel's call.** Two cartridges for the same egg
  declare the same agent name, and `/agents/import` returns `409` and preserves
  the first. That is the conflict check working; pass `--name` to differentiate.
- **A rejected cartridge may still land its egg.** Validation imports the file,
  which runs `__init__`, which lands the (verified) egg — then a later
  conflict check can still reject the agent. The egg on disk is byte-correct
  and the hatcher was never called, so nothing is inconsistent; it is just
  worth knowing before it surprises you.
