# Board Protocol

Shared contract between the mobile app and the ESP32 controller firmware. Both
repositories include this one as a submodule. This document is the single source
of truth: no message may exist in code that is not described here.

**Status: draft v0.1.** Nothing has been implemented against it yet. Expect the
message catalogue to change while the firmware is written; expect the framing and
the versioning rules not to.

## Versioning

`protocolVersion` is `MAJOR.MINOR`.

- MINOR increments for additions that older peers can ignore.
- MAJOR increments for anything that changes or removes an existing shape.
- The app refuses to operate on a MAJOR mismatch and tells the user which side is
  out of date. It warns but continues on a MINOR mismatch.
- Unknown fields are ignored, never rejected. Unknown message types get an
  `unsupported` error, never a disconnect.

Every change to this file bumps the version and gets a line in `CHANGELOG.md`.

## Transport

BLE GATT. The controller is the peripheral, the phone the central.

One central at a time. A second connection attempt is rejected; the app surfaces
this as "board busy" rather than retrying silently.

```
Service            <UUID to be assigned>
  rpc_tx    write             app  -> board   requests
  rpc_rx    notify            board -> app    responses
  events    notify            board -> app    unsolicited state changes
  bulk      notify + write    chunked payloads, both directions
```

The setup Wi-Fi access point is not part of this protocol. It is started on
demand, used by the configuration software only, and BLE is suspended while it
runs so the two do not share the radio.

## Framing

Payloads are JSON, UTF-8, one object per logical message. JSON is chosen over a
binary encoding because the volumes are small and debuggability matters more than
bytes; binary blobs travel through the bulk channel instead.

Messages larger than the negotiated MTU are split. Each fragment:

```
byte 0     message id, low byte
byte 1     message id, high byte
byte 2     fragment index
byte 3     bit 7 = last fragment, bits 0-6 = total fragments
byte 4..   payload slice
```

Request:

```json
{ "id": 7, "op": "setBrightness", "arg": { "value": 180 } }
```

Response:

```json
{ "id": 7, "ok": true,  "result": { "applied": 142, "clamped": true } }
{ "id": 7, "ok": false, "error": { "code": "powerLimit", "message": "..." } }
```

`id` is a monotonically increasing 16-bit counter from the app, echoed unchanged.
Every request gets exactly one response. Default timeout 5 s, except transfers.

## Operations

### hello

No arguments. Must be the first call after connecting.

```json
{ "protocolVersion": "0.1", "firmwareVersion": "1.0.0",
  "boardId": "b3f1...", "name": "Kellerwand",
  "capabilities": ["leds", "routes", "photo", "setupAp"] }
```

`capabilities` is how features are added without breaking older apps. Future
values include `"holdSensing"`. An app must ignore capabilities it does not know
and must not call an operation whose capability is absent.

### getConfigSummary

Cheap freshness check. Called on connect. No polling loop — the controller
notifies on change.

```json
{ "configVersion": 14, "configHash": "…", "photoHash": "…",
  "routeSetVersion": 31 }
```

### getConfig

Full board descriptor. Sent through the bulk channel.

```json
{
  "boardId": "…",
  "name": "Kellerwand",
  "configVersion": 14,
  "layout": {
    "columns": 12, "rows": 12, "spacingMm": 200, "angle": 25,
    "holdSet": null,
    "slots": [ { "holdId": "c00r00", "column": 0, "row": 0, "occupied": true } ]
  },
  "ledMapping": {
    "count": 144, "serpentine": true,
    "origin": "bottomLeft", "direction": "rowMajor",
    "disabled": [37, 38],
    "explicit": null
  },
  "roleColours": { "start": "5FBE7E", "hand": "5C9BE8",
                   "foot": "E0B94A", "finish": "A583E8" },
  "power": { "volts": 5, "milliamps": 3500 },
  "calibration": {
    "anchors": [ {"holdId": "c00r00", "x": 112, "y": 890}, "…3 more" ],
    "homography": [ "…3x3, row major" ],
    "imageWidth": 1600, "imageHeight": 1200
  },
  "capabilities": ["…"],
  "protocolVersion": "0.1"
}
```

`ledMapping.explicit` is an optional full `holdId -> ledIndex` table for boards
whose wiring does not follow a regular serpentine. When present it wins over the
generated mapping.

The app never stores `ledMapping` inside a route. It exists so the adapter can
translate hold IDs at display time.

### setHolds

```json
{ "holds": [ { "holdId": "c03r07", "role": "start" } ] }
```

Roles: `start`, `hand`, `foot`, `finish`. Colours come from `roleColours`; the
app does not send RGB. Unknown hold IDs are reported in the result rather than
failing the whole call:

```json
{ "shown": 11, "unknown": ["c99r99"] }
```

### clear

No arguments.

### setBrightness

`{ "value": 0-255 }`, linear. Gamma correction happens in firmware only.

The controller clamps against the current cap and reports what it actually
applied, so the UI can show that the board is at its limit instead of quietly
disagreeing with the slider.

### setAutoOff

`{ "minutes": 0-240 }`, 0 disables.

### Route storage on the board

```
listRoutes       -> [ { "id": "…", "modifiedAt": "…", "hash": "…",
                        "name": "…", "deviceId": "…" } ]
getRoute         { "id": "…" }        -> route object, bulk if large
putRoute         { "route": { … } }   -> { "stored": true }
deleteRoute      { "id": "…" }
```

Routes are stored in the app's own route format — hold IDs and roles, never LED
indices.

Authority, since there are no accounts:

- `putRoute` records the uploading `deviceId`.
- A device may overwrite or delete routes carrying its own `deviceId`.
- Deleting another device's route, or any bulk wipe, requires `auth` first.

### auth

`{ "pin": "…" }` — the setup PIN configured during assembly. Grants destructive
rights for the duration of the connection. Rate-limited: three failures, then a
60 s lockout.

### Photo

```
getPhotoPreview  -> bulk, JPEG, target <= 25 kB
getPhoto         -> bulk, JPEG, <= 1600 px long edge, <= 400 kB
putPhoto         -> bulk upload, same limits
```

The preview is displayed immediately; the full image downloads in the background
and replaces it. The UI never blocks on the full image.

Replacing the photo changes `photoHash` and raises `photoChanged`. The app then
warns that routes authored against the old photo may be misaligned.

At realistic BLE throughput a 400 kB image takes roughly 15–30 s. Uploads take
longer and must be resumable or cleanly abortable — a half-written photo in flash
is worse than no photo.

### Bulk transfers

```
begin   { "transferId": 3, "op": "getPhoto", "totalBytes": 384102,
          "chunkSize": 512, "crc32": "…" }
chunk   [4-byte header][payload]        on the bulk channel
end     { "transferId": 3, "ok": true }
```

Either side may `abort` with a transfer ID. The receiver verifies CRC before
committing. A failed transfer leaves no partial state.

## Events

Unsolicited, on the events characteristic. No polling.

```json
{ "event": "configChanged",  "configVersion": 15 }
{ "event": "routesChanged",  "routeSetVersion": 32 }
{ "event": "photoChanged",   "photoHash": "…" }
{ "event": "boardState",     "brightness": 142, "displayedRouteId": "…" }
{ "event": "fault",          "code": "…", "message": "…" }
```

## Error codes

```
unsupported     operation or capability not available on this firmware
versionMismatch major protocol version differs
badRequest      malformed payload or invalid argument
notFound        unknown route or hold
forbidden       destructive action without auth
busy            another central is connected, or a transfer is in progress
powerLimit      request exceeded the configured current cap
storageFull     no room for another route or photo
transferFailed  CRC mismatch, timeout or abort
internal        anything else; message is for logs, not for users
```

## Extension rules

New hardware is added as a capability plus its own operations, never by
reshaping existing messages. Hold sensing, for example, becomes
`capabilities: ["holdSensing"]` with its own events — the LED, route and photo
operations stay untouched.

If a change cannot be made this way, it is a MAJOR bump, and it needs a written
reason in `CHANGELOG.md`.
