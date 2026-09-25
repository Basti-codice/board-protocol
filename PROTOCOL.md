# Board Protocol

The contract between the companion app (Flutter) and the board controller (ESP32)
of DIY LED climbing walls.

| | |
|---|---|
| **protocolVersion** | 1 |
| **Document status** | Final for V1, nothing implemented yet |
| **Date** | 2026-09-21 |

This document is the **single source of truth for the interface**. App and
controller are both built against it and tested against the files in
`fixtures/`. Neither side adapts the interface on its own: if code and document
disagree, the document is corrected first (§12), then both sides follow.

Until the first implementation ships, names and type numbers may still change,
but only through this document.

**Contents**

0 Principles · 1 At a glance · 2 Transport · 3 Encoding · 4 Framing ·
5 Addressing and wall layout · 6 Transfers · 7 Versioning and capabilities ·
8 Errors, timeouts and retries · 9 Message catalog · 10 Data models ·
11 Not in V1 · 12 Change process and fixtures · Appendix A Grades ·
Appendix B Open points

---

## 0. Principles

These five rules settle any question this document does not answer. If an
addition contradicts one of them, the addition is wrong, not the rule.

1. **The protocol describes the boundary, not the internals.**
   How the controller is built inside (one chip or two, flash or SD card, task
   layout, pins) does not appear here. Both sides may be rebuilt freely as long
   as the observable behaviour at the boundary stays the same.

2. **The board does not compute routes.**
   The app knows the wall layout and turns grid positions into LED indices. The
   board executes. The board may do more internally, for example limit the
   current or validate input, but the protocol never requires it to render a
   route.

3. **Nothing is hard-coded.**
   LED count, grid, mapping, hold positions, role colours and limits come from
   the board's configuration. An unconfigured board is a valid state.

4. **Stored data never depends on the wiring.**
   Everything that survives a restart is stored in grid positions. Chain indices
   are transient and exist only on the wire.

5. **Unknown things are ignored, not rejected.**
   Unknown JSON fields, capability names, reason strings and set reserved bits
   are not errors. Without this rule no extension would be possible without
   updating every device at once.
   The flip side: a field or bit that changes what a message does is only sent
   when the receiver has announced the matching capability (§7.3). Otherwise an
   old receiver would silently ignore it and do the wrong thing.

---

## 1. At a glance

**Who changes what**

| Data | Changed by | Over BLE |
|---|---|---|
| Settings, wall layout, photo, hold changes | controller menu (admin, at the controller) | read-only |
| Routes stored on the board | any connected app | read and write |
| LED frame, brightness | any connected app, the last input wins | write |

**Why settings are read-only over BLE:** they describe how the wall is built.
They change rarely, and only whoever built or maintains the wall should change
them. Keeping every write path for them in the controller menu means no app can
break a wall's configuration.

**The board as an exchange.** Routes stored on the board are a shared pool
("community routes") that every connected app can browse, display, edit and add
to. Each user's own library lives in the app, per board. Nothing from the board
lands in a user's library unless the user adds it.

**The app computes, the board executes.** Several phones may be connected at the
same time with equal rights; for the LED output the last input wins.

**No access control in V1.** Anyone within Bluetooth range can connect, display
routes, and save, edit or delete board routes. That is accepted for walls at
home; the path to protection is reserved (§11).

**Sessions**

```
First connection                     Later connections
────────────────                     ─────────────────
hello                                hello
describe                             describe
getStatus                            getStatus
getSettings                          getSettings    only if settingsRevision changed
xferBegin layout                     xferBegin …    only for resources whose sha256 changed
xferBegin photoPreview, then photo
syncIndex since 0, when needed       syncIndex      only if routesRevision or storageId changed

Events at any time: statusChanged, wallChanged, disconnecting
```

---

## 2. Transport

### 2.1 GATT service

One GATT service with two characteristics. All messages go through this pair;
they are told apart only by the framing (§4).

| Role | UUID | Properties |
|---|---|---|
| Service | `8ea5ddd4-7713-4d97-a367-cc42ebecf660` | – |
| `RX` (app → board) | `01f118d2-c553-4b08-a7c8-81502f4216c7` | Write, WriteWithoutResponse |
| `TX` (board → app) | `b223e877-25dc-46bf-bf63-854be92cc004` | Notify |

**Why one pair instead of one characteristic per function:** a new
characteristic changes the GATT profile. Old apps would have to rediscover the
service, and every extension would be a break. With a single pair, a new message
type is a pure addition.

**Rules**

* The app subscribes to `TX` before it writes the first message to `RX`.
* After connecting, the app negotiates the largest MTU both sides support.
* In V1 the app writes with **Write** (with response) only.
  `WriteWithoutResponse` is reserved for the future capability `stream` (§11).

### 2.2 Several connections

Several apps may be connected at the same time, up to `maxConnections` from the
settings (§9.3).

* Each connection is independent: own request IDs, own framing state, own idle
  timer, own transfer.
* All connections have the same rights. For the LED output **the last input
  wins**; the other connections learn about it through `wallChanged` (§9.9).
* When no slot is free, the board refuses further connections.
* `maxConnections` always states the value actually in effect. A board may be
  limited to one connection.

**Why several connections:** at a wall with several climbers, everyone should
see what is lit and be able to log attempts on their own phone. With a single
connection, one phone blocks the wall for everyone else.

### 2.3 Advertising and scan data

The board advertises the service UUID. Its scan response carries the board name
and manufacturer-specific data:

```
Offset  Size  Field            Meaning
------  ----  ---------------  ----------------------------------------------
     0     2  companyId        0xFFFF (little-endian), see below
     2     4  boardId          the same 4 bytes as boardId in hello (§3, §9.1)
     6     1  protocolVersion  protocol version of the board
     7     1  freeSlots        connections the board can still accept
```

* Scan data is a **hint** for the app's scan list (known board, busy board,
  incompatible board), so no connection attempt is wasted. The truth always
  comes from `hello`. An app must work when the manufacturer data is missing.
* If the name does not fit into the scan response next to the manufacturer data,
  the board sends a shortened name. The full name comes with `hello`.
* A board with `freeSlots = 0` keeps advertising, so the app can show it as busy
  instead of "not found".
* An unconfigured board advertises like any other board.
* `0xFFFF` is the Bluetooth SIG value for unregistered use. It is a placeholder
  until a registered company ID exists (Appendix B).

**Why the boardId in the scan data:** iOS does not expose Bluetooth addresses,
and the platforms identify devices differently. An ID that comes from the board
itself lets every app recognise a known board before connecting.

---

## 3. Encoding

| Payload | Format | Used for |
|---|---|---|
| Structure | UTF-8 JSON, no BOM | commands, answers, descriptions, errors, events |
| Bulk | raw bytes | LED frames, transfer chunks |

Bit 0 of the `flags` byte tells which one (§4.1). A message always carries
exactly one kind, never both.

**Why mixed:** JSON keeps `fixtures/` readable and diffable, which is the point
of a public contract repository. A full LED frame in JSON would be about five
times the size of its raw bytes and unusable for fast updates. One bit in the
header costs nothing and gives both.

**JSON conventions**

* Field names are `lowerCamelCase` and stable. A field is never renamed, only
  added, or removed in a new `protocolVersion`.
* Numbers are integers. There are no floating-point numbers on the wire.
* Colours are `"#rrggbb"`, lowercase hex.
* Points in time are **Unix seconds** (integer). The board has no clock: the app
  sets timestamps, the board stores and returns them unchanged.
* String limits are given in **UTF-8 bytes**, not characters. An umlaut takes
  2 bytes, an emoji 4.
* Strings are sent as raw UTF-8; `\u` escapes are used only for control
  characters.
* SHA-256 hashes are 64 lowercase hex characters.
* `boardId` and `storageId` are 4 random bytes, written as 8 lowercase hex
  characters in byte order (for example `"a4cf128e"`).
* `ownerId` is 8 random bytes, written as 16 lowercase hex characters.
* `routeId` is an integer in the uint32 range. `0` means "no board route".
* An optional field that is not set is **omitted**, not sent as `null`.
* Unknown fields are ignored (principle 5).

**Binary conventions:** all multi-byte fields are **little-endian**.

---

## 4. Framing

Every message starts with a 5-byte header.

```
Offset  Size  Field    Meaning
------  ----  -------  ------------------------------------------
     0     1  type     message type, see §9
     1     1  id       correlation ID
     2     1  flags    see below
     3     2  length   payload length in bytes (uint16, LE)
     5     L  payload  JSON or raw bytes
```

### 4.1 flags

```
Bit 0     payload    0 = JSON, 1 = binary
Bit 1-2   kind       0 = request
                     1 = response
                     2 = event
                     3 = error
Bit 3-7   reserved   sender sets 0, receiver ignores
```

A message without payload (`length = 0`) is sent with bit 0 = 0.

**kind** is the message's role in the conversation, not its content:

* **request**: expects exactly one answer, a `response` or an `error`.
* **response**: the answer to exactly one request; carries its `type` and `id`.
* **event**: unsolicited, never answered. Allowed in both directions. `id` is 0
  and has no meaning.
* **error**: the answer to a request that was not carried out. Carries the
  request's `type` and `id`; the payload is always an error object (§8.1).

Because an error carries the same `type` as the failed request, there is no
separate error message type and no special matching rule.

### 4.2 id

A running number set by the sender of the request, 0–255, wrapping around. The
answer mirrors it. IDs are **per connection**. The app may have several requests
outstanding as long as their IDs differ.

### 4.3 Messages longer than one packet

A BLE packet holds `MTU − 3` payload bytes, in practice 20–244. A message may be
longer:

* **The header appears once, at the start of the message**, not in every packet.
  The receiver reads the header, knows `length`, and counts the following bytes
  until the message is complete. BLE guarantees order and delivery within a
  connection, so counting is enough.
* **A packet never carries bytes of two messages.** Every message starts at the
  beginning of a packet. Since a packet holds at least 20 bytes, the header
  always arrives complete in the first packet.
* **Messages are never interleaved.** On one connection, a sender finishes a
  message before it starts the next, also when an event becomes due in the
  middle of a long answer.

**Why these rules:** without them an event could end up inside a 4 KB answer,
and a receiver that lost track would have no way to find the next header. With
them, the next packet after any problem always starts with a header.

### 4.4 Size limits

* The board accepts message payloads up to `maxMessage` bytes (§9.2). A longer
  message is answered with `105 messageTooLarge`; the board counts and discards
  its bytes.
* The app accepts every message the `length` field allows (65 535 bytes).
* Anything larger than one message goes through transfers (§6), not through
  larger messages.

### 4.5 Order and precedence

* The board answers the requests of one connection **in the order it received
  them**.
* Pending responses and events are sent **before** the next transfer chunk, so a
  running download never delays other answers (§6.4).

### 4.6 Framing errors

As long as the header is readable, the receiver knows `length` and can **skip a
message it cannot handle without losing track**. That is not a framing error but
a normal error:

* unknown `type` → `103 unknownType` (for an event: ignore silently)
* payload not valid JSON, wrong payload format (JSON instead of binary or the
  other way round), or a required field missing → `104 malformedPayload`
* `length` above `maxMessage` → `105 messageTooLarge`; the bytes are counted and
  discarded

Events are never answered, not even with an error. An event that cannot be
processed is ignored.

`101 framingError` is for an incomplete message: **if a message is incomplete
and no packet arrives for 1 s, the receiver discards it.** If the discarded
message was a request, the board answers with `101`, using the `type` and `id`
from its header. The timeout measures the **gap between packets**, not the total
time: a 4 KB message may legitimately take several seconds on a small MTU.

An app that receives a `1xx` error treats it as a bug on one side (§8.2).

---

## 5. Addressing and wall layout

The wall has two coordinate systems. Which one applies depends on whether data
is stored or transient.

```
Grid position (col, row)             Chain index
  stored, survives every rebuild       on the wire, transient
  says where a hold is                 follows the soldering order
            │                                   ▲
            └───────── ledMap from the layout ──┘
                       computed by the app
```

| Used for | System |
|---|---|
| Routes, wherever they are stored | grid positions `{c, r}` |
| `setLeds` | chain order, 0 … `ledCount − 1` |

**Why separate:** a route may sit in storage for years. If the chain is
re-soldered or a row is turned around, stored chain indices would point at the
wrong holds, with no information left to correct them. Grid positions stay
valid; only the mapping changes, and it lives in the layout. On the wire, the
chain index is exactly what the LED buffer needs, which keeps the board free of
any computation.

### 5.1 Layout

The layout describes the wall position by position. It is a resource the app
downloads (§6) whenever its `sha256` in `getSettings` changes.

```json
{
  "cols": 12,
  "rows": 12,
  "face": { "w": 1800, "h": 2200 },
  "positions":   [[417, 1364], [1250, 1364], "…", null, "…"],
  "ledMap":      [0, 1, 2, "…", 23, 22, "…", null, "…"],
  "holdChanges": [0, 0, 5, 0, "…"]
}
```

`positions`, `ledMap` and `holdChanges` each have `cols × rows` entries in
**reading order**: row 0 is the **top** row, from left to right, then row 1, and
so on.

```
index = row * cols + col
```

| Field | Meaning |
|---|---|
| `cols`, `rows` | grid size |
| `face` | aspect ratio of the wall face, `w : h`, any unit |
| `positions[i]` | centre of the hold as `[x, y]`, normalised to 0–10000 across the wall face (`[0, 0]` top left, `[10000, 10000]` bottom right); `null` = no hold at this position |
| `ledMap[i]` | chain index of the LED at this position; `null` = no LED here |
| `holdChanges[i]` | the `holdsRevision` at which the hold at this position last changed; `0` = never (§5.4) |

Chain indices that no position refers to belong to LEDs that are in the chain
but not used. They always stay dark.

**Why a table and not parameters:** parameters (start corner, snake direction,
skipped LEDs) only describe perfectly regular walls. A skipped position, a row
soldered the wrong way round or a non-rectangular wall cannot be expressed with
them, and every such wall would force a protocol extension. A table can express
everything and costs about 3 KB of JSON for 12 × 12. The parameters are entered
in the controller menu, which generates the table: the generator belongs to the
controller, not to the contract.

**Why normalised positions:** the photo is rectified and cropped to exactly the
wall face in the controller menu (§5.5). The app can therefore draw every hold
directly over the photo without any perspective maths, and the same values draw
a schematic wall when no photo exists. Uneven spacing, for example one row gap
that differs from the others, is simply part of the positions.

### 5.2 Valid route positions

A route may use **every position that has a hold** (`positions[i]` not `null`),
with or without an LED. Holds without an LED are shown in the app only; on the
wall they stay dark.

**Why:** walls often have foothold rows without LEDs, and a route must be able
to mark feet there. Removing an LED later must not make stored routes invalid
(principle 4).

### 5.3 settingsRevision

`settingsRevision` counts every change made in the controller menu: settings,
layout, photo and hold changes alike.

If the app computes a frame with an outdated `ledMap`, the wrong holds light up,
and the board cannot notice, because it only sees indices. Therefore every
`setLeds` carries the `settingsRevision` it was computed against. If it does not
match, the board answers `203 staleSettings` and shows nothing. The app reloads
what changed, recomputes and sends again.

**A settings change switches the wall off.** Whenever `settingsRevision`
changes, the board switches all LEDs off (wall state `none`, §9.4) and sends
`wallChanged` to all connections. A frame computed with the old settings could
show wrong holds or colours, and the board cannot recompute it (principle 2).

`settingsRevision` is a uint16 that wraps around; it is only ever compared for
equality.

### 5.4 holdsRevision and hold changes

When holds are replaced, the wiring stays the same, but routes set before may no
longer be climbable.

* In the controller menu, the admin marks the positions whose holds changed, or
  all positions. The board increases `holdsRevision` by 1 and sets
  `holdChanges[i]` of every marked position to the new value.
* Every route stores the `holdsRevision` its holds were set against: the
  board's `holdsRevision` at the moment the holds were created or last edited.
  The app sets it, also for a route that was created on the phone and published
  later. The board only checks that it is not above its current value (§9.12).
* `holdsRevision` starts at 0 and never decreases.
* A route is **possibly outdated** if at least one of its holds sits on a
  position with `holdChanges[i] > route.holdsRevision`.
* A route hold on a position that no longer has a hold (`positions[i]` is
  `null`, or the position is outside the grid after a rebuild) is **missing**.

Outdated and missing holds are warnings computed by the app. They never block
anything: the route can still be displayed, and missing holds simply stay dark.
A new photo alone never marks a route as outdated.

**Why per position:** with a single global counter, moving one hold would flag
every route on the board, and a warning that is always on gets ignored.

**Why the app sets the route's value:** a route created on the phone before a
hold change and published afterwards must keep its warning. If the board
stamped its current value when saving, the warning would disappear.

### 5.5 Photo

The board can hold a photo of the wall in two sizes:

| Resource | Content |
|---|---|
| `photoPreview` | small version, about 320 px wide |
| `photo` | full resolution |

Both show exactly the wall face, rectified and cropped in the controller menu,
so `positions` apply to them directly. The photo is set only in the controller
menu, always together with the positions; over BLE it is read-only.

**Why a preview:** on the first connection the app can show the wall within one
or two seconds while the full photo loads in the background.

---

## 6. Transfers

For resources that do not fit into one message. V1 has only **downloads**
(board → app); the mechanism is generic.

| Resource | Format |
|---|---|
| `layout` | JSON (§5.1), `mime` = `application/json` |
| `photoPreview` | image, format given by `mime` |
| `photo` | image, format given by `mime` |

**Why not one large message:** a photo of several hundred kilobytes does not fit
into any message buffer. The board streams it from storage in chunks, and chunks
make an interrupted download resumable.

### 6.1 Sequence

```
App                                                   Board
xferBegin  request {resource, offset, chunkSize}  ──▶
           ◀── response {transferId, size, sha256, mime, chunkSize}
           ◀── event xferChunk      (repeated until the end)
           ◀── event xferEnd
App verifies sha256 over the complete resource.
```

### 6.2 Chunk size

```
chunkSize = min( maxChunk , negotiated MTU − 3 − 5 − 5 )

  − 3   BLE ATT overhead
  − 5   message header
  − 5   chunk header (§6.3)
```

The app proposes the value in `xferBegin`; the board confirms it or lowers it.
The value in the response applies. Example: MTU 247 gives 247 − 13 = 234 bytes.

### 6.3 Chunk format

`xferChunk` is an event with a binary payload that starts with its own five
bytes:

```
Offset  Size  Field        Meaning
     0     1  transferId   from the xferBegin response
     1     4  offset       byte position within the whole resource (uint32, LE)
     5     …  data         raw bytes
```

The offset lets the app detect a gap and put each chunk in the right place. The
`transferId` keeps late chunks of an aborted transfer out of a new one.

### 6.4 Rules

* **One transfer per connection** at a time. A second `xferBegin` on the same
  connection is answered with `301 busy`. Different connections may download at
  the same time.
* **Resuming:** `offset` in `xferBegin` is the byte to start at: `0` for a new
  download, the first missing byte to resume. `size` and `sha256` in the
  response always describe the whole resource. If on resuming the `sha256`
  differs from the one the download started with, the resource has changed and
  the app starts again at `0`.
* **No acknowledgements.** The board sends chunks in order as fast as the link
  allows. The app checks every chunk's offset; on a gap it sends `xferAbort` and
  resumes at the first missing byte.
* **Other traffic first:** pending responses and events go out before the next
  chunk (§4.5), so the app stays responsive during a download. `setLeds` is
  never delayed by a running transfer.
* **Stall:** if no chunk arrives for 5 s, the app sends `xferAbort` and may
  resume.
* **Resource changes:** if the resource is replaced during a transfer (for
  example a new photo in the controller menu), the board ends the transfer with
  `xferAbort`, reason `resourceChanged`. A `statusChanged` follows.
* `xferAbort` (event, both directions) ends a transfer at once, without an
  answer.
* A transfer ends with `xferEnd`, with `xferAbort` from either side, or when the
  connection closes.

**Errors on `xferBegin`:** unknown resource → `204 notSupported` · resource not
present (no photo stored) → `202 notFound` · `offset` beyond `size` →
`201 invalidArgument` · unconfigured board → `302 notConfigured`.

**Why no acknowledgements:** uploads are not part of V1. For downloads the phone
is the fast side, BLE delivers in order, and the offset check plus the SHA-256
catch everything else. This removes windows, acknowledgements and their
timeouts from both sides.

---

## 7. Versioning and capabilities

Two questions, two separate mechanisms:

| Question | Mechanism |
|---|---|
| Do we read the same bytes? | `protocolVersion` (one number) |
| What can you do? | `capabilities` (list of names) |

### 7.1 protocolVersion

A single integer, currently `1`. It increases **only** when existing software
would misread new bytes. If the versions differ, the session ends; there is no
partial compatibility between versions.

**Frozen forever:** the header layout (§4) and `hello` (§9.1). Whatever else
changes, every app can talk to every board long enough to find out that the
versions differ.

**On a mismatch** the board answers `hello` with `102 unsupportedVersion`,
carrying its own version as a field (§9.1). The app can then say precisely what
needs updating, the app or the board, and disconnects.

**Increases the version:**

* a change to the header layout, field sizes or byte order
* reusing a `type` or `kind` value that is already assigned
* renaming, removing or changing the meaning of a JSON field
* tightening a rule that was allowed before

**Does not increase the version:**

* a new `type`
* a new optional JSON field
* a new capability
* a new error code within an existing range
* using a reserved bit, provided that `0` keeps the old behaviour and §7.3 is
  followed

### 7.2 capabilities

A list of names the board reports in `describe`. The app checks features against
this list, never against a version number.

**Why no minor version:** a version number only says how old a firmware is. It
cannot say what hardware is attached: a brand-new board without sensors would
have a high minor version and still could not report touches. Capabilities
answer the actual question and cover hardware differences too.

V1 defines:

| Name | Meaning |
|---|---|
| `leds` | `setLeds`, `setBrightness`, wall state, `wallChanged` |
| `routes` | route storage: `syncIndex`, `getRoutes`, `saveRoute`, `updateRoute`, `deleteRoute` |
| `photo` | the board can store a photo (`photoPreview`, `photo`) |

Reserved for later, named here so they are not assigned twice: `sensors`,
`animations`, `stream`, `localPlayback`, `ota`, `bulkChannel`, `auth`,
`ownership`, `ascents` (§11).

An unknown capability name is ignored (principle 5).

### 7.3 Fields and bits that change behaviour

Principle 5 is only safe when ignoring something is harmless. Therefore:

> A field or bit that changes the effect of a message is only sent when the
> receiver has announced the matching capability. Fields that only carry
> information may always be sent.

**Example:** a later flag asking the board to fade a frame in is harmless if an
old board ignores it, so it may always be sent. A later field that would let
`saveRoute` replace an existing route would, if ignored, silently create a
duplicate, so it may only be sent to boards that announce the matching
capability.

---

## 8. Errors, timeouts and retries

### 8.1 Error object

An error is an answer with `kind = 3`, the same `type` and the same `id` as the
request. Its payload is JSON:

```json
{ "code": 203, "message": "settingsRevision 11, expected 12" }
```

* `code` is binding.
* `message` is **optional and for debugging only**. The app may log it, but
  never shows it to users and never parses it. The text is not part of the
  contract and may change at any time.
* Some codes carry additional fields; they are listed with the code.

### 8.2 Ranges

The hundreds digit tells the app what to do, even for a code it does not know.

| Range | Meaning | App reaction |
|---|---|---|
| `1xx` | protocol error | a bug on one side: log, disconnect, reconnect; do not repeat the request (`102`: §9.1) |
| `2xx` | request error | the request was wrong; repeating it unchanged does not help |
| `3xx` | board state | not possible right now; try again later |
| `4xx` | storage | tell the user, cancel the operation |

### 8.3 Codes in V1

```
1xx  Protocol
 101  framingError         incomplete message, discarded (§4.6)
 102  unsupportedVersion   protocolVersion differs; carries "protocolVersion" (§9.1)
 103  unknownType          type unknown
 104  malformedPayload     invalid JSON, wrong payload format, required field missing
 105  messageTooLarge      length above maxMessage

2xx  Request
 201  invalidArgument      value out of range (details with each message)
 202  notFound             route or resource does not exist
 203  staleSettings        settingsRevision outdated: reload, recompute, resend
 204  notSupported         capability or resource not available on this board
 205  conflict             route changed since baseRev (§9.13)
 206  unauthorized         reserved for the capability auth, not used in V1

3xx  State
 301  busy                 a transfer is already running on this connection
 302  notConfigured        the board has no configuration yet

4xx  Storage
 401  storageFull          no space left for this route
```

New codes are additions within a range and do not increase `protocolVersion`.

### 8.4 Timeouts

| What | Value | Who |
|---|---|---|
| Gap between packets of one message | 1 s | both (§4.6) |
| Answer to a request | 3 s until its first packet arrives; 10 s for `saveRoute`, `updateRoute` and `deleteRoute`, because they write to storage | app: the request counts as failed and the app closes the connection |
| Gap between transfer chunks | 5 s | app (§6.4) |
| Idle connection | `idleTimeoutS` from the settings | board (§9.6) |

A request's timeout starts once the answers to all earlier requests of the same
connection have arrived. The board answers in order (§4.5), so a slow route
change must not make the requests queued behind it time out.

**Why the app closes the connection after a missing answer:** request IDs wrap
around at 256 (§4.2), so a late answer could be matched to a newer request with
the same ID. A new connection starts clean; repeating the request follows §8.5.
A finer rule would be possible (an ID is free again once a later request has
been answered), but it is not worth it: in practice a missing answer means the
link is broken, and reconnecting is the simplest robust reaction.

### 8.5 Repeating requests

A request that failed (timeout, lost connection) may be repeated, but only where
repeating cannot do harm:

* **Always safe:** all reading requests, `setLeds`, `setBrightness`. `setLeds`
  always carries a full frame, so a repeated or newer frame repairs any earlier
  failure.
* **Route changes** (`saveRoute`, `updateRoute`, `deleteRoute`) carry a
  `token`: a random uint32 the app creates once per operation and reuses for
  every repetition of that operation. The board remembers the answers to the
  last 16 tokens, across all connections and across restarts. A request with a
  known token gets the remembered answer and is not carried out a second time.
  The token is stored in the same step as the change it belongs to: after a
  power loss, either both exist or neither.
* **Never repeated unchanged:** requests that failed with `1xx`, `2xx` or
  `4xx`. (`203` and `205` lead to a changed request, see below.)

**Why tokens:** if the connection drops after the board saved a route but before
the answer arrived, the app cannot know whether the save happened. Without a
token, repeating it would create a duplicate; for an update, the repetition
would even report a false conflict. The tokens survive a restart because a
restart between saving and answering has the same effect.

**Special handling**

* `203 staleSettings`: reload what changed, recompute, send again, without
  asking the user.
* `205 conflict`: the user decides (§9.13).
* `301 busy`: wait for the running transfer to finish, then try again.

---

## 9. Message catalog

### Type numbers

```
0x00        reserved
0x01-0x0F   session, description, status
0x10-0x1F   LEDs and wall state
0x20-0x2F   routes
0x30-0x3F   transfers
0x40-0x4F   reserved for sensors
0x50-0xFF   free
```

| type | Name | kind | Direction | Capability |
|---|---|---|---|---|
| `0x01` | `hello` | request / response | app → board | – |
| `0x02` | `describe` | request / response | app → board | – |
| `0x03` | `getSettings` | request / response | app → board | – |
| `0x04` | `getStatus` | request / response | app → board | – |
| `0x05` | `statusChanged` | event | board → app | – |
| `0x06` | `disconnecting` | event | board → app | – |
| `0x10` | `setLeds` | request / response | app → board | `leds` |
| `0x11` | `setBrightness` | request / response | app → board | `leds` |
| `0x12` | `wallChanged` | event | board → app | `leds` |
| `0x20` | `syncIndex` | request / response | app → board | `routes` |
| `0x21` | `getRoutes` | request / response | app → board | `routes` |
| `0x22` | `saveRoute` | request / response | app → board | `routes` |
| `0x23` | `updateRoute` | request / response | app → board | `routes` |
| `0x24` | `deleteRoute` | request / response | app → board | `routes` |
| `0x30` | `xferBegin` | request / response | app → board | – (`photo` for the photo resources) |
| `0x31` | `xferChunk` | event | board → app | – |
| `0x32` | `xferEnd` | event | board → app | – |
| `0x33` | `xferAbort` | event | both | – |

**General rules**

* The app sends `hello` first and nothing else until it has the answer.
* On an unconfigured board, every request except `hello`, `describe`,
  `getSettings` and `getStatus` is answered with `302 notConfigured`.

---

### 9.1 `0x01` hello (frozen)

The first message after connecting. Deliberately tiny: if the versions differ,
that is clear after a few dozen bytes, and the app never has to parse a
`describe` whose layout it might not understand.

**Request**
```json
{ "protocolVersion": 1, "client": "kb-app/0.3.1" }
```
`client` is optional and only for the board's log.

**Response**
```json
{ "protocolVersion": 1, "boardId": "a4cf128e", "name": "Basement" }
```

| Field | Meaning |
|---|---|
| `boardId` | 4 random bytes the board creates once, on its first start, and replaces only with a factory reset. Identifies the board permanently; the name may change. |
| `name` | board name, set in the controller menu, ≤ 32 bytes; an unconfigured board reports a default name |

**Error:** if `protocolVersion` differs → `102 unsupportedVersion`:
```json
{ "code": 102, "protocolVersion": 1 }
```
`protocolVersion` in the error is the board's own version. The app disconnects
afterwards.

**Why a random boardId instead of the Bluetooth address:** the address is a
hardware detail (principle 1) and changes when the chip is replaced. A random ID
can be carried over with a configuration backup.

---

### 9.2 `0x02` describe

What does not change during the board's lifetime, except through a firmware
update. Once per session.

**Request:** empty (`length = 0`)

**Response**
```json
{
  "boardId": "a4cf128e",
  "firmware": "1.0.0",
  "hardware": "esp32-wroom-32",
  "capabilities": ["leds", "routes", "photo"],
  "maxMessage": 4096,
  "maxChunk": 512,
  "maxRouteHolds": 64,
  "maxBatch": 10
}
```

| Field | Meaning |
|---|---|
| `firmware`, `hardware` | informational only; the app never decides anything based on them |
| `capabilities` | §7.2 |
| `maxMessage` | largest message payload the board accepts (§4.4). Always large enough for a full `setLeds` (`7 + 3 × ledCount` bytes) and for a `saveRoute` or `updateRoute` with `maxRouteHolds` holds and every field at its limit; the board never accepts a configuration that breaks this |
| `maxChunk` | largest chunk data size the board sends (§6.2) |
| `maxRouteHolds` | most holds per route |
| `maxBatch` | most route IDs per `getRoutes` (§9.11) |

---

### 9.3 `0x03` getSettings

How the wall is built. Changes rarely and only in the controller menu:
**read-only over BLE.** There is deliberately no `setSettings`.

**Request:** empty

**Response**
```json
{
  "configured": true,
  "name": "Basement",
  "settingsRevision": 12,
  "holdsRevision": 5,
  "ledCount": 108,
  "roles": {
    "start":  "#00c853",
    "hand":   "#2962ff",
    "foot":   "#ffd600",
    "finish": "#aa00ff"
  },
  "power": { "budgetMa": 4000, "maPerChannel": 20 },
  "angleAdjustable": false,
  "maxConnections": 3,
  "idleTimeoutS": 600,
  "ledsOffAfterS": 1800,
  "layout": { "size": 3120, "sha256": "…" },
  "photo": {
    "preview": { "size": 21480,  "sha256": "…", "mime": "image/jpeg", "width": 320,  "height": 391 },
    "full":    { "size": 301226, "sha256": "…", "mime": "image/jpeg", "width": 1600, "height": 1956 }
  }
}
```

| Field | Meaning |
|---|---|
| `configured` | `false` on an unconfigured board; then only `settingsRevision` is present besides it |
| `name` | board name as in `hello` (§9.1); a rename reaches connected apps through this message |
| `settingsRevision` | §5.3 |
| `holdsRevision` | §5.4 |
| `ledCount` | LEDs in the chain, including unused ones; a frame has exactly this many entries |
| `roles` | colour per hold role; V1 roles: `start`, `hand`, `foot`, `finish` |
| `power.budgetMa` | current the LED supply may deliver, in mA |
| `power.maPerChannel` | current of one colour channel at full value, in mA |
| `angleAdjustable` | `true` if the wall angle can be changed; routes then carry an angle |
| `maxConnections` | connections the board accepts at the same time (§2.2) |
| `idleTimeoutS` | the board closes a connection after this many seconds without a message from it; `0` = never (§9.6) |
| `ledsOffAfterS` | the LEDs go off this many seconds after the last connection closed; `0` = never |
| `layout` | size and SHA-256 of the layout resource (§5.1) |
| `photo` | size, SHA-256, format and pixel size of both photo resources (§5.5); absent if no photo is stored |

Unconfigured board:
```json
{ "configured": false, "settingsRevision": 0 }
```

**Roles, not colours:** routes store the role of each hold, never its colour. A
colour changed in the controller menu applies to every route at once, and no
route becomes wrong.

**Caching:** the app keeps the layout and the photos and loads a resource again
only when its `sha256` changes.

**Current estimate:** the app may estimate the current of a frame before sending
it and give the user a hint:

```
current ≈ Σ over all LEDs  (r + g + b) / 255 × maPerChannel × brightness / 255
```

The estimate is only a hint. The board enforces the budget itself (§9.7). The
estimate deliberately ignores the small quiescent current of the LEDs; the
board includes it when it enforces the budget.

---

### 9.4 `0x04` getStatus

What changes all the time. The app calls it on **every** connect and reloads
settings, resources or the route index only if the matching revision,
`storageId` or hash has changed.

**Request:** empty

**Response**
```json
{
  "settingsRevision": 12,
  "routesRevision": 87,
  "storageId": "5be0a913",
  "routeCount": 143,
  "storageFree": 1043968,
  "brightness": 160,
  "wall": { "source": "app", "routeId": 42, "wallSeq": 3108 },
  "uptime": 90210
}
```

| Field | Meaning |
|---|---|
| `settingsRevision` | §5.3 |
| `routesRevision` | increases with every route change on the board (§9.10) |
| `storageId` | 4 random bytes, new whenever the route storage is formatted. The app keys its cached route index on it: after a format, equal revision numbers may mean different content. |
| `routeCount`, `storageFree` | informational; `storageFree` in bytes |
| `brightness` | current global brightness, 0–255 |
| `wall.source` | where the current frame came from: `"app"` (a `setLeds`), `"button"` (shown again with the button on the controller), or `"none"` when all LEDs are off |
| `wall.routeId` | board route currently shown; `0` = the wall shows no board route (for example a private or unsaved route); always `0` when `source` is `"none"` |
| `wall.wallSeq` | uint32 that counts changes of the frame: +1 for every `setLeds`, for the button and whenever the LEDs go off; a brightness change leaves it unchanged. Starts at a random value below 2³¹ at every start of the board, so values are comparable only within one run of the board (§9.9) |
| `uptime` | seconds since start, informational |

After every start of the board, the wall is off: `source` is `"none"`.

**Why three description messages:** the split follows the rate of change, not
the topic. The most frequent case, reconnecting and checking whether anything
happened, costs one small message instead of the full settings.

---

### 9.5 `0x05` statusChanged (event)

Sent by the board to **all** connections whenever `settingsRevision`,
`routesRevision` or `storageId` changes, for example after another phone saved a
route or after a change in the controller menu.

```json
{ "settingsRevision": 13, "routesRevision": 88, "storageId": "5be0a913" }
```

The app reloads what changed, exactly as after `getStatus`. Because of this
event the app never needs to poll.

---

### 9.6 `0x06` disconnecting (event)

Sent by the board right before it closes a connection on its own.

```json
{ "reason": "idle" }
```

V1 reason: `idle`, meaning no message from this connection for `idleTimeoutS`
seconds. Unknown reasons are shown generically.

**Rules**

* `idleTimeoutS = 0` means the board never closes idle connections.
* A running transfer counts as activity.
* The board's timeout always wins over any app setting.
* **Apps do not send messages just to keep a connection alive.** Otherwise the
  timeout would never take effect, and a forgotten phone could block a
  connection slot indefinitely.

---

### 9.7 `0x10` setLeds

Sets a complete frame. Binary payload (flags bit 0 = 1).

```
Offset  Size  Field             Meaning
     0     2  settingsRevision  revision the frame was computed against (uint16, LE)
     2     1  reserved          0
     3     4  routeId           board route shown; 0 = no board route (uint32, LE)
     7   N×3  rgb               three bytes R, G, B per LED, in chain order
                                N = ledCount from getSettings
```

For 108 LEDs that is 331 bytes, one message.

**Response**
```json
{ "limited": false, "wallSeq": 3108 }
```
`limited` is `true` if the board had to dim the output to stay within its
current budget. `wallSeq` is the wall's sequence number after this frame
(§9.4, §9.9).

**Errors:** `203 staleSettings` if the revision does not match ·
`201 invalidArgument` if the payload is not exactly `7 + ledCount × 3` bytes ·
`302 notConfigured` on an unconfigured board.

**The board then**

* shows the frame, scaled by the global brightness,
* keeps the total current within `power.budgetMa` by dimming, never by refusing,
* sets the wall state: `source = "app"` and `routeId` from the frame; if every
  LED is off, `source = "none"` and `routeId = 0`. `wallSeq` increases by 1,
* sends `wallChanged` to all **other** connections.

A running transfer never delays `setLeds`.

**Rendering a route:** every hold of the route that has an LED gets exactly the
colour of its role from `roles`. Every other LED is `0`. Brightness is applied
by the board, not in the frame. (Other content, for example feedback
while editing, may use any colours.)

**One frame at a time:** an app has at most one `setLeds` outstanding per
connection. Frames produced meanwhile replace each other; only the newest is
sent next. This keeps the delay short however fast the user taps, for example in
the editor's live mode, and nothing piles up.

**Why only full frames:** there is deliberately no variant that sets only a few
LEDs. At 108 LEDs the difference is about 200 bytes, and a full frame leaves no
doubt about what is on the wall afterwards: every successful frame repairs any
earlier failure. Turning everything off means sending a frame of zeros.

**Why the routeId:** other phones learn which board route is lit, can show it,
and let their users log attempts on it. The board only passes the ID on; it
does not look the route up.

> **Checking:** because this message is binary, `fixtures/` holds an annotated
> hex dump next to the raw file.

---

### 9.8 `0x11` setBrightness

**Request**
```json
{ "value": 120 }
```

`0`–`255`. Applies globally and immediately, does not change the frame, and is
kept across restarts. The board stays within its current budget as with
`setLeds` and sends `wallChanged` to all other connections.

**Response**
```json
{ "limited": false }
```

**Error:** `201 invalidArgument` if the value is outside 0–255.

---

### 9.9 `0x12` wallChanged (event)

Tells an app that something else changed what is on the wall.

```json
{ "source": "app", "routeId": 42, "wallSeq": 3108, "brightness": 160 }
```

Sent to every connection except the one whose `setLeds` or `setBrightness`
caused it. When the board changes the wall itself (button, settings change),
sent to all connections. The fields have the same meaning as `wall` and
`brightness` in `getStatus` (§9.4).

**Who owns the wall:** an app knows that another device or the board has
changed the frame when it receives a `wallSeq` newer than the one in the answer
to its own last `setLeds`. A `wallChanged` with an unchanged `wallSeq` only
reports a brightness change.

After connecting, an app keeps the `wallSeq` it remembered from an earlier
connection only if it equals `wall.wallSeq` in `getStatus`. Otherwise another
device, the board or a restart has changed the wall in the meantime, and the app
discards the remembered value.

**Why a sequence number:** answers and events of different connections can
cross. Without `wallSeq`, a phone whose frame was applied last could still
receive the `wallChanged` of an earlier frame from another phone and wrongly
conclude that it had lost the wall.

---

### 9.10 `0x20` syncIndex

Keeps the app's route index up to date, transferring only what changed. The
index holds the metadata needed for search and filters (§10.2), not the holds.

Every route change on the board (save, update, delete) increases
`routesRevision` by 1 and gives the changed route that value as its `rev`. A
deleted route leaves a **tombstone** with the `rev` of its deletion.
`routesRevision` is 0 on a new, empty route storage and never decreases while
the `storageId` stays the same.

**Request**
```json
{ "since": 87 }
```
`since` is the revision the app's index is at; `0` for a complete index.

**Response**
```json
{
  "routesRevision": 90,
  "reset": false,
  "entries": [
    { "routeId": 42, "rev": 88, "name": "Sunrise", "grade": "6A+", "…": "…" },
    { "routeId": 17, "rev": 90, "deleted": true }
  ],
  "more": false
}
```

**Rules**

* `entries` holds every route and tombstone with `rev > since`, sorted by `rev`,
  ascending. Each route appears once, in its latest state; revisions in between
  may be missing because a later change replaced them.
* The board decides how many entries fit on one page, at least one if any
  remain. If `more` is `true`, the app asks again with `since` set to the `rev`
  of the last entry received.
* Changes made while the app is paging get higher revisions and arrive on later
  pages; nothing is skipped.
* The board keeps only a limited number of tombstones. If `since` is older than
  the oldest tombstone it still has, or larger than `routesRevision`, the board
  answers with `reset: true` and the complete index as for `since = 0`, without
  tombstones. The app then discards its index before applying the entries.
* If `storageId` has changed (§9.4), the app discards its index and starts at
  `since = 0`.

**Why the app filters and not the board:** search and every filter run on the
app's index. The board would be slower, and every new filter criterion would be
a protocol change. The index costs about 150 bytes per route; after the first
sync, only changes are transferred.

---

### 9.11 `0x21` getRoutes

Loads complete routes including their holds, for previews and for display.

**Request**
```json
{ "routeIds": [42, 17, 103] }
```
At most `maxBatch` IDs.

**Response**
```json
{
  "routes": [ { "routeId": 42, "rev": 88, "…": "…" } ],
  "missing": [17, 103]
}
```

`routes` holds complete route objects (§10.1). `missing` lists IDs that do not
exist (any more); that is not an error. Copies in users' libraries are not
affected.

**Error:** `201 invalidArgument` if more than `maxBatch` IDs are requested.

---

### 9.12 `0x22` saveRoute

Stores a new route on the board.

**Request**
```json
{
  "token": 2718281828,
  "ownerId": "9c41e07ad2b35f18",
  "route": {
    "name": "Sunrise",
    "grade": "6A+",
    "setter": "Sebastian",
    "description": "Left heel on the start jug.",
    "createdAt": 1790000000,
    "updatedAt": 1790000000,
    "holdsRevision": 5,
    "feet": "marked",
    "tags": ["crimpy"],
    "holds": [
      { "c": 3, "r": 8, "role": "start" },
      { "c": 7, "r": 0, "role": "finish" }
    ]
  }
}
```

**Response**
```json
{ "routeId": 144, "rev": 88 }
```

**Rules**

* **The board assigns the `routeId`.** Otherwise two phones could independently
  create the same ID and overwrite each other. Route IDs start at 1 and are never
  reused: a deleted ID never comes back.
* The board sets `rev` (§9.10). `holdsRevision` comes from the app (§5.4).
* `token`: §8.5.
* `ownerId`: a random ID each app installation creates once. The board stores it
  with the route. In V1 it is neither evaluated nor returned; it prepares
  owner-only editing (capability `ownership`, §11), so that routes saved in V1
  already have an owner. It is never returned, on purpose: once owner-only
  editing exists, knowing a route's `ownerId` would be enough to act as its
  owner.
* In V1, every connected app may save.

**Errors**

* `201 invalidArgument`: a hold outside the grid or on a position without a hold
  (§5.2), the same position twice, an unknown role, no hold at all, more than
  `maxRouteHolds` holds, an empty name, `holdsRevision` above the board's
  current value, or a field beyond its limit (§10.1)
* `401 storageFull`: no space left
* `302 notConfigured`

---

### 9.13 `0x23` updateRoute

Changes an existing route on the board. In V1 every connected app may change
every route.

**Request**
```json
{
  "token": 1414213562,
  "routeId": 144,
  "baseRev": 88,
  "route": { "name": "Sunrise", "…": "…" }
}
```

* `route` is the complete route as it should be stored: it replaces every field
  that §10.1 marks as set by the app, and an optional field that is omitted is
  removed. `createdAt` and the stored `ownerId` are kept; `routeId` and `rev`
  are never taken from the request.
* `baseRev` is the `rev` of the version the edit started from.

**Response**
```json
{ "rev": 92 }
```

**Errors**

* `205 conflict`: the route has changed since `baseRev`. The app asks the user:
  **overwrite** (load the current route with `getRoutes`, send again with its
  `rev` as `baseRev` and a new `token`, because it is a new operation) or
  **discard**.
* `202 notFound`: the route was deleted in the meantime.
* `201`, `401`, `302` as for `saveRoute`.

**Why `baseRev`:** two people editing the same route would otherwise overwrite
each other without noticing.

---

### 9.14 `0x24` deleteRoute

**Request**
```json
{ "token": 1732050807, "routeId": 144 }
```

**Response**
```json
{ "rev": 93 }
```

Leaves a tombstone with this `rev` (§9.10). In V1 every connected app may delete
every route. Copies in users' libraries are not affected.

**Error:** `202 notFound`.

---

### 9.15 `0x30`–`0x33` Transfers

Sequence and rules: §6. The payloads:

**`xferBegin` request**
```json
{ "resource": "photo", "offset": 0, "chunkSize": 234 }
```

**`xferBegin` response**
```json
{ "transferId": 3, "size": 301226, "sha256": "…", "mime": "image/jpeg", "chunkSize": 234 }
```

**`xferChunk` event:** binary, §6.3.

**`xferEnd` event** (board → app, after the last chunk)
```json
{ "transferId": 3 }
```

**`xferAbort` event** (both directions)
```json
{ "transferId": 3, "reason": "resourceChanged" }
```
Reasons in V1: `userCancelled`, `gap`, `resourceChanged`. Unknown reasons are
treated like any other abort.

---

## 10. Data models

### 10.1 Route

```json
{
  "routeId": 42,
  "rev": 88,
  "name": "Sunrise",
  "grade": "6A+",
  "setter": "Sebastian",
  "description": "Left heel on the start jug.",
  "createdAt": 1790000000,
  "updatedAt": 1790086400,
  "holdsRevision": 4,
  "angle": 40,
  "feet": "marked",
  "tags": ["crimpy", "slab"],
  "holds": [
    { "c": 3, "r": 8,  "role": "start"  },
    { "c": 5, "r": 6,  "role": "hand"   },
    { "c": 4, "r": 10, "role": "foot"   },
    { "c": 7, "r": 0,  "role": "finish" }
  ]
}
```

| Field | Type | Required | Set by | Rules |
|---|---|---|---|---|
| `routeId` | int | yes | board | §9.12; never reused |
| `rev` | int | yes | board | §9.10 |
| `name` | string | yes | app | 1–48 bytes |
| `grade` | string | no | app | Font grade from Appendix A; omitted = no grade |
| `setter` | string | no | app | ≤ 32 bytes |
| `description` | string | no | app | ≤ 280 bytes; the setter's note, visible to everyone |
| `createdAt` | int | yes | app | Unix seconds; set once by `saveRoute` |
| `updatedAt` | int | yes | app | Unix seconds |
| `holdsRevision` | int | yes | app | §5.4; not above the board's current value |
| `angle` | int | no | app | 0–90; wall angle in degrees the route was set for; apps set it only on walls with `angleAdjustable` |
| `feet` | string | no | app | foot rule, see below; omitted = `marked` |
| `tags` | string[] | no | app | ≤ 8 tags, each ≤ 24 bytes |
| `holds` | object[] | yes | app | 1 … `maxRouteHolds`; `c` column, `r` row, `role` from `roles`; each position at most once; positions per §5.2 |

The example's `foot` hold at row 10 sits in a foothold row without LEDs (§5.2).

Route text fields (`name`, `setter`, `description`, `tags`) contain no control
characters, except line feed in `description`.

**`feet`**: which footholds may be used.

| Value | Meaning |
|---|---|
| `marked` | only the holds marked as feet |
| `kicker` | the marked feet plus all footholds at the bottom of the wall |
| `any` | every hold may be used as a foothold |

The board stores the rule without evaluating it; it is information for
climbers.

**`grade`** is always stored as a Font grade. Apps display V grades by
converting with Appendix A; an empty grade stays empty.

**Stored is the role, not the colour** (§9.3).

**Personal data never goes to the board:** personal notes, logs and attempts
stay in the app.

### 10.2 Index entry

What `syncIndex` delivers per route: the fields of §10.1 **without**
`description` and `holds`, plus `holdCount`.

```json
{
  "routeId": 42, "rev": 88, "name": "Sunrise", "grade": "6A+",
  "setter": "Sebastian", "createdAt": 1790000000, "updatedAt": 1790086400,
  "holdsRevision": 4, "angle": 40, "feet": "marked",
  "tags": ["crimpy", "slab"], "holdCount": 4
}
```

A tombstone carries only `routeId`, `rev` and `"deleted": true`.

**Why `holdsRevision` in the index:** if it equals the board's current
`holdsRevision`, no hold of the route has changed since its holds were set, and
the app knows that without loading the holds.

---

## 11. Not in V1

Deliberately left out. Every item is an addition of messages and capabilities;
none requires a new `protocolVersion`.

| Topic | Planned path |
|---|---|
| Sensors (touch detection) | capability `sensors`, events in `0x40`–`0x4F`; the app processes them and answers with frames |
| Animations, games with many frames per second | capabilities `stream` / `animations`: frames as events without an answer, over `WriteWithoutResponse` |
| Showing stored routes without a phone | capability `localPlayback`; the board renders routes itself |
| Only the owner may change or delete a route | capability `ownership`; `ownerId` is stored from V1 on |
| PIN / authentication | capability `auth`, error `206 unauthorized` |
| Ascent counters per route | capability `ascents` |
| Second channel pair for bulk data | capability `bulkChannel` |
| Firmware update over BLE | not planned: firmware is updated in the controller menu over Wi-Fi; the name `ota` stays reserved |
| Uploading the photo over BLE | not planned: the photo is set in the controller menu |
| Changing settings over BLE | deliberately never: settings belong to the controller menu |
| Searching on the board | deliberately never: the app filters its index (§9.10) |

---

## 12. Change process and fixtures

1. Every change to this document is recorded in `CHANGELOG.md`.
2. If behaviour on the wire changes, the matching files in `fixtures/` are added
   or updated in the same change.
3. `protocolVersion` increases only by the rules in §7.1. A version bump is a
   commit of its own that changes nothing else.
4. The document version (Git tag, `CHANGELOG.md`) is **not** `protocolVersion`.
   The document may change ten times while the contract on the wire stays the
   same.
5. No implementation changes the interface on its own. If code and document
   disagree, this document is corrected first, then both sides follow.

### fixtures/

Example messages that **both sides test against**; that is the actual purpose of
this repository. Every message type has at least one valid example; messages
with error cases also have an invalid one.

Examples in this document are illustrative; the files in `fixtures/` are
consistent with each other and are the reference for tests.

Fixtures use the **reference wall**. It is test data, never an assumption in
code:

* wall face 1800 × 2200 mm (width × height)
* 12 columns × 12 rows, centred horizontally and vertically on the face; hold
  spacing 150 mm, except 100 mm between the two top rows (row 0 and row 1)
* every position has a hold; the three bottom rows have no LEDs, which leaves
  108 LEDs
* chain: a snake starting top left, seen from the front: row 0 from left to
  right, row 1 from right to left, and so on downwards
* role colours as in the `getSettings` example (§9.3); current budget 4000 mA,
  20 mA per colour channel

```
fixtures/
  README.md                             format of every fixture family
  scan/
    scanResponse.bin                    manufacturer data (§2.3)
    scanResponse.hexdump.txt
  0x01-hello.request.json
  0x01-hello.response.json
  0x01-hello.unsupportedVersion.error.json
  0x02-describe.response.json
  0x03-getSettings.response.json
  0x03-getSettings.unconfigured.response.json
  0x04-getStatus.response.json
  0x05-statusChanged.event.json
  0x06-disconnecting.event.json
  0x10-setLeds.request.bin
  0x10-setLeds.request.hexdump.txt
  0x10-setLeds.response.json
  0x10-setLeds.staleSettings.error.json
  0x11-setBrightness.request.json
  0x11-setBrightness.response.json
  0x12-wallChanged.event.json
  0x20-syncIndex.request.json
  0x20-syncIndex.response.json          with a tombstone and more = true
  0x20-syncIndex.reset.response.json
  0x21-getRoutes.request.json
  0x21-getRoutes.response.json          with missing IDs
  0x22-saveRoute.request.json
  0x22-saveRoute.response.json
  0x22-saveRoute.invalidPosition.error.json
  0x23-updateRoute.request.json
  0x23-updateRoute.response.json
  0x23-updateRoute.conflict.error.json
  0x24-deleteRoute.request.json
  0x24-deleteRoute.response.json
  0x30-xferBegin.request.json
  0x30-xferBegin.response.json
  0x31-xferChunk.event.bin
  0x31-xferChunk.event.hexdump.txt
  0x32-xferEnd.event.json
  0x33-xferAbort.event.json
  errors/                               one error per V1 code not covered above,
                                        0xTT-<message>.<code>-<name>.error.json
  resources/
    layout.json                         reference wall before any hold change
    layout.holdChanges.json             same wall after hold changes (holdsRevision 5);
                                        the layout resource of getSettings
    layout.holdRemoved.json             same wall with one hold removed
    photoPreview.png                    32 × 39 px
    photo.png                           160 × 196 px
  derived/
    route-to-frame/                     route + layout + roles → expected setLeds payload
    route-warnings/                     route + layout → expected outdated / missing holds
  frames/                               complete messages incl. 5-byte header,
                                        split into packets for MTU 23 and MTU 247
  grades.json                           Appendix A as data
tools/
  generate_fixtures.py                  generates fixtures/ and checks it
```

`derived/` covers what is computed from protocol data. It keeps every
implementation, for example a later local playback on the board, computing
exactly the same.

---

## Appendix A: Grades

**Stored** is always a Font grade from this list:

`4` `4+` `5` `5+` `6A` `6A+` `6B` `6B+` `6C` `6C+` `7A` `7A+` `7B` `7B+` `7C`
`7C+` `8A` `8A+` `8B` `8B+` `8C` `8C+` `9A`

Uppercase letters, `+` without a space. Apps validate the format; the board does
not.

**Conversion to and from V**

| V | Font grades shown as this V | Stored when entered as this V |
|---|---|---|
| V0 | 4, 4+ | 4 |
| V1 | 5 | 5 |
| V2 | 5+ | 5+ |
| V3 | 6A, 6A+ | 6A |
| V4 | 6B, 6B+ | 6B |
| V5 | 6C, 6C+ | 6C |
| V6 | 7A | 7A |
| V7 | 7A+ | 7A+ |
| V8 | 7B, 7B+ | 7B |
| V9 | 7C | 7C |
| V10 | 7C+ | 7C+ |
| V11 | 8A | 8A |
| V12 | 8A+ | 8A+ |
| V13 | 8B | 8B |
| V14 | 8B+ | 8B+ |
| V15 | 8C | 8C |
| V16 | 8C+ | 8C+ |
| V17 | 9A | 9A |

* A grade entered as V is stored as the value in the last column. Converting it
  back gives the same V grade.
* The conversion between the scales is approximate by nature. This table is part
  of the contract so that every app converts identically.

---

## Appendix B: Open points

| Point | Why open |
|---|---|
| Route change time | How long a route change takes on nearly full storage, including the token (§8.5), has to be measured. It must stay below the 10 s of §8.4. |
| `maxMessage = 4096` in the examples | Has to be checked against the real memory use of JSON handling on the controller. According to the fixtures, the largest possible route message is about 3 350 bytes: `saveRoute` or `updateRoute` with `maxRouteHolds` holds and every field at its limit, as compact JSON (`tools/generate_fixtures.py`). |
| Download speed | A full photo of about 300 KB has to be measured on real hardware. If it is too slow: `bulkChannel` (§11). |
| `companyId` | `0xFFFF` is a placeholder until a registered company ID exists. Changing it only affects the optional scan data (§2.3). |
