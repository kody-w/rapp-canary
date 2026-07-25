# Cartridges — an `.egg` that travels inside an `agent.py`

```bash
python3 rapp_brainstem/tools/cartridge.py pack thing.egg
#   -> thing_cartridge_agent.py   (one file)
```

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

1. **verify** — SHA-256 of the embedded egg, checked before anything is written
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
