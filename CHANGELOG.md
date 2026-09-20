# Changelog

All notable changes to the wire format live here. Every change to `PROTOCOL.md`
bumps `protocolVersion` and gets a line in this file.

`protocolVersion` is `MAJOR.MINOR`:

- MINOR increments for additions that older peers can ignore.
- MAJOR increments for anything that changes or removes an existing shape, and
  needs a written reason in the entry below.

## 0.2

Closes the gaps that made 0.1 unimplementable. Most of these changes are
MAJOR-shaped: they change or remove existing shapes. 0.1 was never implemented
by either side, so no compatibility is owed, and the version stays inside the
0.x draft range instead of going to 1.0.

Transport

- Service and characteristic UUIDs assigned. Advertising carries the service
  UUID; the scan response carries the name and manufacturer data with `boardId`
  and a status byte (connected, unconfigured).
- MTU: the app requests 517, the assumed minimum is 185, and `hello` fails
  below it.
- Just Works bonding with encryption required for everything except `hello`.

Framing

- Fragment header changed: byte 3 is now the fragment count, and the bit-7 last
  flag is gone. The message id is a transport-level uint16 counter, independent
  of the envelope `id`. At most 255 fragments per message; anything larger uses
  the bulk channel.

Bulk transfers

- Chunk header defined: `transferId` and sequence number, both uint16
  little-endian.
- The `begin` message is removed; the response of the triggering operation
  announces the transfer. `end` becomes the `transferEnd` event.
- Credit-based flow control for downloads, with the new `credit` operation.
- `abort` defined; it was referenced but undefined in 0.1. The controller
  aborts by sending `transferEnd` with `ok: false`, because it cannot write to
  `rpc_tx`.
- Failed and aborted transfers leave no partial state. 0.2 does not resume
  transfers.

Operations

- `hello` takes arguments (`deviceId`, `appVersion`, `protocolVersion`) and
  returns `unconfigured`, `limits` and `mtu`.
- New: `credit`, `abort`, `getBoardState`, `startSetupAp`, `getCalibration`.
- New, HTTP only: `putConfig`, `importRoutes`, `otaUpdate`.
- `setHolds` gains an optional `routeId` that sets `displayedRouteId`, always
  carries the complete set of holds, and reports `applied` and `clamped`.
- `putPhoto` redefined: calibration, photo and preview in one request and one
  upload, committed atomically.
- `listRoutes` is now a download transfer, because 250 entries exceed 255
  fragments. `getRoute` is always a plain response, wrapped as
  `{ "route": … }`.
- `auth` keeps its lockout across reconnects and reboots, with backoff doubling
  from 60 s to a cap of 15 minutes.

Data

- Route schema defined, with the limits `maxRoutes` (provisional),
  `maxRouteBytes` and `maxHoldsPerRoute` returned in `hello`.
- Hashing defined: CRC-32 everywhere, over canonical JSON for route hashes and
  over raw bytes for config and photo. Canonical JSON written as a procedure.
- Timestamps are opaque app-supplied strings; the controller never parses or
  generates them.
- `layoutRev` added to config, to `getConfigSummary` and to routes. Route
  staleness follows hold positions, not the photo.
- `ledMapping.disabled` removed: `layout.slots[].occupied` is the single source
  of truth. The mapping procedure is specified, and `ledMapping.faulty` is added
  for dead LEDs that are skipped only when rendering.
- `layout.spacingMm` replaced by `columnSpacingMm` and `rowOffsetsMm`.
- `calibration` removed from `getConfig`; it travels with the photo and is read
  with `getCalibration`.
- Power model specified with exact integer arithmetic, plus
  `power.controllerSharesSupply`.

Errors

- `powerLimit` removed. The controller never refuses to light a route on power
  grounds: it scales brightness down and reports `clamped: true`, so nothing
  could ever raise the code.

Setup transport

- New section: HTTP `POST /rpc` over the setup access point, sharing the
  operation handlers with BLE. Bulk payloads travel as HTTP bodies, the setup
  PIN grants a session token under the same lockout, and routes stored this way
  belong to the setup owner `0000000000000000`.

Fixtures

- `fixtures/` populated with framing, canonical JSON and message vectors. The
  format is defined in `fixtures/README.md`.

## 0.1

- Initial draft.
