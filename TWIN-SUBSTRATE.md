# Universal twin substrate flight

This flight tests the additive `rapp/2-twin` parent contract without changing
the Brainstem kernel.

`parent_rappid` remains lineage: the twin from which another twin descends.
The new `parent` block names the subject that a twin represents, while the new
`substrate` block records the local evidence sources that ground it.

The parent has a closed `nature` (`virtual`, `physical`, or `hybrid`) and an
open `class` such as `person`, `place`, `repo`, `device`, `vehicle`, or any
future subject. Every class uses the same substrate engine; source types vary.

The drop-in `TwinSubstrate` cartridge can:

- designate a parent while preserving an existing `rapp/1-twin` manifest;
- bind transcript, prompt, Git, filesystem, shell, CSV, media, or JSONL sources;
- harvest those sources into one local SQLite FTS event stream;
- search, recall, and render timelines with line-accurate, versioned pointers;
- open only pointers indexed for the selected twin, after revalidating the
  source boundary, file identity, and content digest; and
- roll back a source harvest atomically if any file changes or fails mid-read.

The flight is local-first and stdlib-only. Its state is isolated under
`~/.brainstem-flights/universal-twin-substrate/twins`, including direct bulk
CLI runs from this checkout. It does not modify `brainstem.py`, the installer,
`VERSION`, `soul.md`, or dependency manifests.
