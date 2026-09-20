# board-protocol

The single source of truth for the wire format between the **app** and the
**controller firmware** of a DIY LED climbing board.

This repository contains no code. It contains the contract.

## Contents

| File | What it is |
|---|---|
| `PROTOCOL.md` | The specification. Transport, framing, message catalogue, error codes, versioning and extension rules. |
| `CHANGELOG.md` | One entry per version. Every change to `PROTOCOL.md` appears here. |
| `fixtures/` | Codec test vectors. Both sides decode these in their own unit tests. |

## How it is used

Both repositories include this one as a git submodule:

- the Flutter app, at `protocol/`
- the ESP32 firmware, at its own path

Neither side may define a message that is not described in `PROTOCOL.md`.

## The rule

**The protocol changes here first.**

1. Edit `PROTOCOL.md`.
2. Bump `protocolVersion` — MINOR for a backwards-compatible addition, MAJOR for
   anything that changes or removes an existing shape.
3. Add an entry to `CHANGELOG.md`. A MAJOR bump needs a written reason.
4. Add or update fixtures in `fixtures/` for anything that changes on the wire.
5. Only then update the app and the firmware, and move their submodule pointer.

Never add, rename or reshape a message in app or firmware code first. A message
that exists in code but not in this document is a bug in both repositories.

## Fixtures

Codec test vectors live in `fixtures/` so that the app and the firmware are
tested against the same bytes rather than against each other's assumptions.

The format of each fixture family is defined in fixtures/README.md: framing,
canonical JSON and one file per operation.

## Status

**Draft v0.3.** Nothing is implemented against it yet. Expect the message
catalogue to change while the firmware is written; expect the framing and the
versioning rules not to.
