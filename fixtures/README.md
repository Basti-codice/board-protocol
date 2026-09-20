# Fixtures

Codec test vectors for `PROTOCOL.md` v0.2. The app and the firmware both load
every file here in their unit tests, so each side is tested against the same
bytes rather than against the other's assumptions.

All files are UTF-8 JSON. Hex strings are lowercase without separators. If a
fixture and `PROTOCOL.md` disagree, the specification wins and the fixture is a
bug: report it, do not adjust either side silently.

## framing/*.json

Fragmentation of one message on `rpc_tx`, `rpc_rx` or `events`.

| Field | Meaning |
|---|---|
| `description` | what the case covers |
| `mtu` | negotiated ATT MTU; payload per fragment is `mtu - 7` |
| `messageId` | transport message id written into bytes 0-1 |
| `message` | the logical message as a string; its UTF-8 bytes are what is fragmented |
| `messageBytes` | UTF-8 length of `message`, for convenience |
| `fragments` | array of fragments, each one hex string of the whole fragment (4-byte header followed by the payload slice), in send order. `null` when the message cannot be framed |
| `error` | only on negative cases: `"tooManyFragments"` |

A test must check both directions: fragmenting `message` yields exactly
`fragments`, and reassembling `fragments` yields exactly the bytes of `message`.

## canonical/*.json

Canonical JSON serialisation and CRC-32 (PROTOCOL.md, "Canonical JSON" and
"Hashing").

| Field | Meaning |
|---|---|
| `description` | the rule the case covers |
| `input` | a JSON value. Decode it with the platform's JSON parser, then serialise it canonically |
| `canonical` | the expected canonical serialisation, as a string. Compare its UTF-8 bytes |
| `crc32` | CRC-32 of the UTF-8 bytes of `canonical` |
| `error` | only on negative cases: `"notCanonical"`; `canonical` and `crc32` are then `null` and the serialiser must fail |

Route cases (files `2x-route-*`) add:

| Field | Meaning |
|---|---|
| `input` | the route **without** `hash`, so `crc32` is the route's `hash` |
| `routeBytes` | UTF-8 length of the canonical JSON of the route **with** `hash` inserted. This is the value checked against `maxRouteBytes` |
| `nameCharacters`, `nameBytes` | where the case is about the name: its length in code points and in UTF-8 bytes |

`24-route-one-byte-over-max` is canonically well defined but invalid for
`putRoute`, at 2049 bytes against a `maxRouteBytes` of 2048.

## messages/*.json

One file per operation, named after the `op`.

| Field | Meaning |
|---|---|
| `description` | what the operation does |
| `op` | operation name |
| `transport` | `["ble"]`, `["http"]` or both, as in the operation table |
| `request` | a complete request envelope |
| `responseOk` | a complete success response envelope for that request |
| `responseError` | a complete error response envelope for that request |

`_events.json` holds one example of every event on the `events` characteristic.

Message fixtures are shape examples: decode each envelope, check that every
field has the type the specification defines, and re-encode it. Hashes and
transfer sizes inside them are illustrative and not cross-checked.

## Coverage

Framing: single fragment, exactly-full fragment, one byte over, three
fragments, a multi-byte UTF-8 character split across a fragment boundary,
MTU 517, 255 fragments (the maximum) and 256 fragments (rejected).

Canonical JSON: every rule on its own (key sorting by UTF-8 bytes against both
locale and UTF-16 order, recursion, non-ASCII keys, whitespace, quote and
backslash escaping, lowercase control escapes, unescaped slash, raw non-ASCII,
integers, literals, array order, empty containers, float rejection, a non-object
top level), plus routes: a basic route, a multi-byte name, a 48-character name
of 4-byte characters, a route of exactly `maxRouteBytes` and one a byte over.

Messages: all 23 operations of v0.2, and all events.
