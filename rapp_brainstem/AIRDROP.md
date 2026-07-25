# AirDrop — the link cable between two devices

A RAPP capability is one self-contained `agent.py`. So moving a capability
between devices is moving one file, and AirDrop already does that well. This is
the two ends of that cable, wired to endpoints the brainstem already had.

**No kernel change.** `GET /agents/export/<file>` and `POST /agents/import`
existed already; this adds the send/receive surface to `index.html`.

## Sending

Agents panel → **AirDrop** next to any agent.

- Where a **share sheet** is available, the OS sheet opens with the real
  `agent.py` attached and AirDrop is in it.
- Where it is not, the file is downloaded and the panel tells you the rest:
  **Files → long-press → Share → AirDrop**.

## Receiving

Agents panel → **Receive an agent** → pick the AirDropped file.

Drag & drop already existed, but there is no drag & drop on a phone, so an
AirDropped file had no way in. That is the gap this closes.

The browser sends a SHA-256 of the bytes it read, and the brainstem checks the
digest against what it received — a file truncated in transit is refused rather
than half-installed. The agent is live on your next message.

## The one thing that will surprise you

`navigator.share()` with files requires a **secure context**.
`http://localhost` counts. **`http://192.168.x.x` does not.**

So a phone browsing the brainstem across the LAN cannot open the share sheet,
no matter how many times you tap. The panel detects this on open and says so
before you tap, rather than after.

In practice:

| From | To | Path |
|---|---|---|
| the machine running the brainstem (`localhost`) | any device | share sheet → AirDrop, one tap |
| a phone on the LAN | any device | AirDrop saves the file → share it from Files |

Both directions work. Only one of them is one tap.

## The iOS gesture trap

iOS discards the user-gesture context across an `await`. Fetching the file and
then calling `share()` in the same handler throws `NotAllowedError` on iPhone
while working perfectly on a Mac — a bug that only appears on the device you
built it for.

The file is therefore fetched on `pointerdown` and shared on `click`, so the
`await` resolves an already-in-flight promise and the gesture survives. If it
throws anyway, the code falls back to the download path with the reason logged.
