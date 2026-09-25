#!/usr/bin/env python3
"""Generates fixtures/ from PROTOCOL.md (§12).

Usage:  python tools/generate_fixtures.py

Deletes and rewrites everything under fixtures/ except fixtures/README.md, then
reads the files back and checks them. Python 3, standard library only.

The output is byte-identical on every run. The only exception across machines
are the PNG resources: their IDAT data comes from zlib, whose output may differ
between zlib versions. The committed files are authoritative (fixtures/README.md).
"""

import hashlib
import json
import re
import shutil
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
KEEP = {"README.md"}

# ---------------------------------------------------------------------------
# Reference wall (PROTOCOL.md §12) and the values of the examples in §9
# ---------------------------------------------------------------------------

COLS = 12
ROWS = 12
FACE_W = 1800                       # mm
FACE_H = 2200                       # mm
SPACING = 150                       # mm between neighbouring holds
TOP_SPACING = 100                   # mm between row 0 and row 1
LED_ROWS = 9                        # rows 9, 10, 11 have no LEDs
LED_COUNT = COLS * LED_ROWS         # 108

PROTOCOL_VERSION = 1
BOARD_ID = "a4cf128e"
BOARD_NAME = "Basement"
STORAGE_ID = "5be0a913"
SETTINGS_REVISION = 12
HOLDS_REVISION = 5
ROLES = {
    "start": "#00c853",
    "hand": "#2962ff",
    "foot": "#ffd600",
    "finish": "#aa00ff",
}
MAX_CONNECTIONS = 3
MAX_MESSAGE = 4096
MAX_CHUNK = 512
MAX_ROUTE_HOLDS = 64
MAX_BATCH = 10
WALL_SEQ = 3108
BRIGHTNESS = 160
FREE_SLOTS = 2

# Hold changes of layout.holdChanges.json: (c, r) -> holdsRevision of the change.
HOLD_CHANGES = {(8, 3): 5, (5, 6): 5, (6, 2): 4, (2, 10): 3}
# layout.holdRemoved.json: the hold at this position (it has an LED) is removed.
REMOVED_HOLD = (6, 2)

PHOTO_RGB = (0x80, 0x80, 0x80)
PREVIEW_PX = (32, 39)
PHOTO_PX = (160, 196)
MIME_PNG = "image/png"

XFER_MTU = 247
TRANSFER_ID = 3
CHUNK_SIZE = min(MAX_CHUNK, XFER_MTU - 3 - 5 - 5)    # §6.2: 234

FRAME_MTUS = (23, 247)

FONT_GRADES = [
    "4", "4+", "5", "5+", "6A", "6A+", "6B", "6B+", "6C", "6C+", "7A", "7A+",
    "7B", "7B+", "7C", "7C+", "8A", "8A+", "8B", "8B+", "8C", "8C+", "9A",
]
V_GRADES = [
    ("V0", ["4", "4+"], "4"),
    ("V1", ["5"], "5"),
    ("V2", ["5+"], "5+"),
    ("V3", ["6A", "6A+"], "6A"),
    ("V4", ["6B", "6B+"], "6B"),
    ("V5", ["6C", "6C+"], "6C"),
    ("V6", ["7A"], "7A"),
    ("V7", ["7A+"], "7A+"),
    ("V8", ["7B", "7B+"], "7B"),
    ("V9", ["7C"], "7C"),
    ("V10", ["7C+"], "7C+"),
    ("V11", ["8A"], "8A"),
    ("V12", ["8A+"], "8A+"),
    ("V13", ["8B"], "8B"),
    ("V14", ["8B+"], "8B+"),
    ("V15", ["8C"], "8C"),
    ("V16", ["8C+"], "8C+"),
    ("V17", ["9A"], "9A"),
]

ERROR_NAMES = {
    101: "framingError", 102: "unsupportedVersion", 103: "unknownType",
    104: "malformedPayload", 105: "messageTooLarge", 201: "invalidArgument",
    202: "notFound", 203: "staleSettings", 204: "notSupported", 205: "conflict",
    206: "unauthorized", 301: "busy", 302: "notConfigured", 401: "storageFull",
}

KINDS = {"request": 0, "response": 1, "event": 2, "error": 3}
TYPE_NAMES = {
    0x01: "hello", 0x02: "describe", 0x03: "getSettings", 0x04: "getStatus",
    0x05: "statusChanged", 0x06: "disconnecting", 0x10: "setLeds",
    0x11: "setBrightness", 0x12: "wallChanged", 0x20: "syncIndex",
    0x21: "getRoutes", 0x22: "saveRoute", 0x23: "updateRoute",
    0x24: "deleteRoute", 0x30: "xferBegin", 0x31: "xferChunk", 0x32: "xferEnd",
    0x33: "xferAbort",
}

# ---------------------------------------------------------------------------
# Encoding helpers
# ---------------------------------------------------------------------------


def json_file(obj):
    """Fixture file form: UTF-8, 2-space indentation, LF, final newline."""
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def json_wire(obj):
    """Wire form used in frames/: compact, same key order."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def header(type_, id_, kind, binary, length):
    flags = (1 if binary else 0) | (KINDS[kind] << 1)
    return struct.pack("<BBBH", type_, id_, flags, length)


# ---------------------------------------------------------------------------
# Layout (§5.1)
# ---------------------------------------------------------------------------


def col_mm(c):
    margin = FACE_W - (COLS - 1) * SPACING
    assert margin % 2 == 0
    return margin // 2 + c * SPACING


def row_mm(r):
    grid_h = TOP_SPACING + (ROWS - 2) * SPACING
    margin = FACE_H - grid_h
    assert margin % 2 == 0
    top = margin // 2
    return top if r == 0 else top + TOP_SPACING + (r - 1) * SPACING


def normalise(mm, total):
    """mm across the face -> 0..10000, rounded to the nearest integer."""
    num = mm * 10000
    assert 2 * (num % total) != total, "exact half: rounding would be ambiguous"
    return (2 * num + total) // (2 * total)


def chain_index(c, r):
    """Snake from the top left, seen from the front; bottom rows have no LEDs."""
    if r >= LED_ROWS:
        return None
    return r * COLS + (c if r % 2 == 0 else COLS - 1 - c)


def make_layout(hold_changes=None, removed=()):
    hold_changes = hold_changes or {}
    positions, led_map, changes = [], [], []
    for r in range(ROWS):
        for c in range(COLS):
            gone = (c, r) in removed
            positions.append(None if gone else [normalise(col_mm(c), FACE_W),
                                                normalise(row_mm(r), FACE_H)])
            led_map.append(None if gone else chain_index(c, r))
            changes.append(hold_changes.get((c, r), 0))
    return {
        "cols": COLS,
        "rows": ROWS,
        "face": {"w": FACE_W, "h": FACE_H},
        "positions": positions,
        "ledMap": led_map,
        "holdChanges": changes,
    }


def layout_file(layout):
    """Like json_file, but the three grid arrays get one grid row per line."""
    cols, rows = layout["cols"], layout["rows"]
    lines = [
        "{",
        f'  "cols": {cols},',
        f'  "rows": {rows},',
        '  "face": {',
        f'    "w": {layout["face"]["w"]},',
        f'    "h": {layout["face"]["h"]}',
        "  },",
    ]
    grids = ["positions", "ledMap", "holdChanges"]
    for n, name in enumerate(grids):
        values = layout[name]
        lines.append(f'  "{name}": [')
        for r in range(rows):
            cells = ", ".join(json.dumps(v) for v in values[r * cols:(r + 1) * cols])
            lines.append("    " + cells + ("," if r < rows - 1 else ""))
        lines.append("  ]" + ("," if n < len(grids) - 1 else ""))
    lines.append("}")
    data = ("\n".join(lines) + "\n").encode("utf-8")
    assert json.loads(data) == layout
    return data


def grid_index(layout, c, r):
    """Index into the grid arrays, or None if (c, r) is outside the grid."""
    if 0 <= c < layout["cols"] and 0 <= r < layout["rows"]:
        return r * layout["cols"] + c
    return None


# ---------------------------------------------------------------------------
# Photo resources (PNG, one colour)
# ---------------------------------------------------------------------------


def png(width, height, rgb):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data)))

    raw = (b"\x00" + bytes(rgb) * width) * height
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


# ---------------------------------------------------------------------------
# Routes (§10.1, §10.2)
# ---------------------------------------------------------------------------


def route(name, holds, holds_revision, created_at, updated_at=None, *,
          route_id=None, rev=None, grade=None, setter=None, description=None,
          feet=None, tags=None):
    r = {}
    if route_id is not None:
        r["routeId"] = route_id
    if rev is not None:
        r["rev"] = rev
    r["name"] = name
    if grade is not None:
        r["grade"] = grade
    if setter is not None:
        r["setter"] = setter
    if description is not None:
        r["description"] = description
    r["createdAt"] = created_at
    r["updatedAt"] = created_at if updated_at is None else updated_at
    r["holdsRevision"] = holds_revision
    if feet is not None:
        r["feet"] = feet
    if tags is not None:
        r["tags"] = tags
    r["holds"] = [{"c": c, "r": row, "role": role} for c, row, role in holds]
    return r


def index_entry(r):
    entry = {k: v for k, v in r.items() if k not in ("description", "holds")}
    entry["holdCount"] = len(r["holds"])
    return entry


# The route of §10.1, without angle (the reference wall is not adjustable).
ROUTE_42 = route(
    "Sunrise",
    [(3, 8, "start"), (5, 6, "hand"), (4, 10, "foot"), (7, 0, "finish")],
    4, 1790000000, 1790086400, route_id=42, rev=88, grade="6A+",
    setter="Sebastian", description="Left heel on the start jug.",
    feet="marked", tags=["crimpy", "slab"])

ROUTE_3 = route(
    "Warm-up Ladder",
    [(1, 8, "start"), (2, 6, "hand"), (1, 4, "hand"), (2, 2, "hand"), (1, 0, "finish")],
    0, 1788500000, route_id=3, rev=7, grade="5", setter="Mia", feet="any")

ROUTE_58 = route(
    "Grüne Leiste",
    [(2, 7, "start"), (4, 7, "start"), (3, 5, "hand"), (6, 4, "hand"),
     (5, 2, "hand"), (6, 0, "finish")],
    5, 1789200000, 1789286400, route_id=58, rev=91, grade="6C+",
    setter="Jörg", description="Stay low through the middle.", feet="any",
    tags=["crimpy", "technical"])

SAVE_ROUTE = route(
    "Sunrise",
    [(3, 8, "start"), (7, 0, "finish")],
    5, 1790000000, grade="6A+", setter="Sebastian",
    description="Left heel on the start jug.", feet="marked", tags=["crimpy"])

UPDATE_ROUTE = route(
    "Sunrise",
    [(3, 8, "start"), (5, 6, "hand"), (4, 10, "foot"), (7, 0, "finish")],
    5, 1790000000, 1790086400, grade="6A+", setter="Sebastian",
    description="Left heel on the start jug.", feet="marked",
    tags=["crimpy", "slab"])

# ---------------------------------------------------------------------------
# derived/ cases
# ---------------------------------------------------------------------------

# name -> (routeId, route, expected lit LEDs {chain index: role})
ROUTE_TO_FRAME = {
    "ordinary": (58, ROUTE_58, {
        93: "start", 91: "start", 68: "hand", 54: "hand", 29: "hand", 6: "finish"}),
    "allRoles": (61, route(
        "Four Colours",
        [(1, 7, "start"), (3, 8, "foot"), (2, 5, "hand"), (4, 3, "hand"), (3, 1, "finish")],
        5, 1789500000, route_id=61, rev=80, grade="6A", setter="Mia",
        feet="marked"), {
        94: "start", 99: "foot", 69: "hand", 43: "hand", 20: "finish"}),
    "holdWithoutLed": (42, ROUTE_42, {99: "start", 77: "hand", 7: "finish"}),
    "chainEnds": (73, route(
        "Corner to Corner",
        [(11, 8, "start"), (6, 4, "hand"), (0, 0, "finish")],
        5, 1789600000, route_id=73, rev=85, grade="7B", feet="kicker"), {
        107: "start", 54: "hand", 0: "finish"}),
    "onlyHoldsWithoutLeds": (80, route(
        "Floor Traverse",
        [(0, 10, "start"), (3, 9, "hand"), (6, 10, "hand"), (9, 9, "hand"),
         (11, 11, "finish")],
        5, 1789700000, route_id=80, rev=86, grade="5+", feet="any"), {}),
    "routeIdZero": (0, route(
        "Draft",
        [(4, 8, "start"), (5, 5, "hand"), (4, 1, "finish")],
        5, 1790100000, feet="marked"), {
        100: "start", 66: "hand", 19: "finish"}),
    "routeIdNonZero": (0x12345678, route(
        "Byte Order",
        [(8, 8, "start"), (9, 6, "hand"), (10, 3, "hand"), (9, 0, "finish")],
        5, 1789800000, route_id=0x12345678, rev=90, grade="6B+"), {
        104: "start", 81: "hand", 37: "hand", 9: "finish"}),
}

# name -> (layout resource, route, expected outdated, expected missing)
ROUTE_WARNINGS = {
    "noWarning": ("layout.holdChanges.json", route(
        "Clean Line",
        [(1, 8, "start"), (1, 5, "hand"), (3, 3, "hand"), (2, 0, "finish")],
        2, 1788800000, route_id=12, rev=40, grade="5+"), [], []),
    "outdated": ("layout.holdChanges.json", ROUTE_42, [(5, 6)], []),
    "notOutdatedEqualRevision": ("layout.holdChanges.json", route(
        "Heel Hook",
        [(1, 7, "start"), (2, 10, "foot"), (3, 4, "hand"), (4, 0, "finish")],
        3, 1789000000, route_id=21, rev=52, grade="6B"), [], []),
    "missingHoldRemoved": ("layout.holdRemoved.json", route(
        "Gone Pinch",
        [(5, 8, "start"), (6, 2, "hand"), (7, 0, "finish")],
        5, 1789900000, route_id=66, rev=83, grade="6C"), [], [(6, 2)]),
    "missingOutsideGrid": ("layout.holdChanges.json", route(
        "Old Arete",
        [(3, 8, "start"), (12, 4, "hand"), (7, 0, "finish")],
        5, 1789950000, route_id=67, rev=84, grade="7A"), [], [(12, 4)]),
    "outdatedAndMissing": ("layout.holdRemoved.json", route(
        "Everything Changed",
        [(7, 0, "finish"), (5, 6, "hand"), (12, 5, "hand"), (6, 2, "hand"),
         (8, 3, "hand"), (2, 8, "start")],
        3, 1789100000, route_id=31, rev=60, grade="7A+"),
        [(8, 3), (5, 6)], [(6, 2), (12, 5)]),
}

# ---------------------------------------------------------------------------
# Computations defined by the protocol (the thing derived/ tests)
# ---------------------------------------------------------------------------


def render_route(settings_revision, route_id, r, layout, settings):
    """§9.7: every hold with an LED gets the colour of its role, all else 0."""
    rgb = bytearray(3 * settings["ledCount"])
    for h in r["holds"]:
        i = grid_index(layout, h["c"], h["r"])
        if i is None or layout["positions"][i] is None:
            continue
        led = layout["ledMap"][i]
        if led is None:
            continue
        rgb[3 * led:3 * led + 3] = bytes.fromhex(settings["roles"][h["role"]][1:])
    return struct.pack("<HBI", settings_revision, 0, route_id) + bytes(rgb)


def route_warnings(r, layout):
    """§5.4: missing wins over outdated; both in reading order."""
    outdated, missing = [], []
    for h in r["holds"]:
        pos = {"c": h["c"], "r": h["r"]}
        i = grid_index(layout, h["c"], h["r"])
        if i is None or layout["positions"][i] is None:
            missing.append(pos)
        elif layout["holdChanges"][i] > r["holdsRevision"]:
            outdated.append(pos)
    key = lambda p: (p["r"], p["c"])
    return {"outdated": sorted(outdated, key=key), "missing": sorted(missing, key=key)}


# ---------------------------------------------------------------------------
# Annotated hex dumps
# ---------------------------------------------------------------------------


def hexdump(title, fields, packet_mtus=()):
    """fields: list of (bytes, label, show_ascii). Lines hold at most 16 bytes,
    never cross a field or a packet boundary. Everything after '#' is comment."""
    total = sum(len(f[0]) for f in fields)
    markers = {}
    for mtu in packet_mtus:
        for n, off in enumerate(range(0, total, mtu - 3)):
            markers.setdefault(off, []).append(f"mtu{mtu}/{n:03}.bin")
    width = max(4, len(f"{total:x}"))
    lines = [("# " + t).rstrip() for t in title]
    lines += ["#", "# offset: bytes  # meaning", "#"]
    off = 0
    for data, label, show_ascii in fields:
        pos, end = off, off + len(data)
        first = True
        while pos < end:
            stop = min(end, pos + 16)
            inner = [m for m in markers if pos < m < stop]
            if inner:
                stop = min(inner)
            if pos in markers:
                lines.append("# ---- packet " + ", ".join(markers[pos]))
            part = data[pos - off:stop - off]
            comment = label if first else ""
            if show_ascii:
                view = "".join(chr(b) if 32 <= b < 127 else "." for b in part)
                comment = (comment + "  " if comment else "") + "|" + view + "|"
            line = f"{pos:0{width}x}: " + " ".join(f"{b:02x}" for b in part)
            if comment:
                line = f"{line:<{width + 2 + 47}}  # {comment}"
            lines.append(line)
            pos, first = stop, False
        off = end
    return ("\n".join(lines) + "\n").encode("utf-8")


def parse_hexdump(data):
    out = bytearray()
    for line in data.decode("utf-8").split("\n"):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        off, hexpart = line.split(":", 1)
        assert int(off, 16) == len(out), f"offset {off} does not follow on"
        out += bytes.fromhex(hexpart)
    return bytes(out)


def setleds_fields(payload, layout, settings):
    rev, reserved, route_id = struct.unpack_from("<HBI", payload)
    fields = [
        (payload[0:2], f"settingsRevision = {rev} (uint16 LE)", False),
        (payload[2:3], f"reserved = {reserved}", False),
        (payload[3:7], f"routeId = {route_id} = 0x{route_id:08x} (uint32 LE)", False),
    ]
    where = {led: (i % layout["cols"], i // layout["cols"])
             for i, led in enumerate(layout["ledMap"]) if led is not None}
    role_of = {colour: role for role, colour in settings["roles"].items()}
    for led in range((len(payload) - 7) // 3):
        rgb = payload[7 + 3 * led:10 + 3 * led]
        pos = f"c{where[led][0]} r{where[led][1]}" if led in where else "unused"
        colour = "#" + rgb.hex()
        state = "off" if colour == "#000000" else f"{colour} {role_of.get(colour, '')}".rstrip()
        fields.append((rgb, f"led {led:3}  {pos:<7} {state}", False))
    return fields


def chunk_fields(payload):
    transfer_id, offset = struct.unpack_from("<BI", payload)
    return [
        (payload[0:1], f"transferId = {transfer_id}", False),
        (payload[1:5], f"offset = {offset} (uint32 LE)", False),
        (payload[5:], f"data, {len(payload) - 5} bytes", True),
    ]


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def clean():
    FIXTURES.mkdir(exist_ok=True)
    for p in sorted(FIXTURES.iterdir()):
        if p.name in KEEP:
            continue
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()


def write(files, rel, data):
    assert rel not in files, rel
    path = FIXTURES / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    files[rel] = data


def generate():
    files = {}

    # -- resources -----------------------------------------------------------
    layout = make_layout()
    layout_changed = make_layout(HOLD_CHANGES)
    layout_removed = make_layout(HOLD_CHANGES, removed={REMOVED_HOLD})
    layouts = {
        "layout.json": layout,
        "layout.holdChanges.json": layout_changed,
        "layout.holdRemoved.json": layout_removed,
    }
    for name, value in layouts.items():
        write(files, f"resources/{name}", layout_file(value))
    preview = png(*PREVIEW_PX, PHOTO_RGB)
    photo = png(*PHOTO_PX, PHOTO_RGB)
    write(files, "resources/photoPreview.png", preview)
    write(files, "resources/photo.png", photo)
    current_layout = files["resources/layout.holdChanges.json"]

    # -- scan data (§2.3) ----------------------------------------------------
    scan = struct.pack("<H", 0xFFFF) + bytes.fromhex(BOARD_ID) + bytes([PROTOCOL_VERSION, FREE_SLOTS])
    write(files, "scan/scanResponse.bin", scan)
    write(files, "scan/scanResponse.hexdump.txt", hexdump(
        ["scanResponse.bin: manufacturer-specific data of the scan response",
         "(PROTOCOL.md §2.3), 8 bytes, companyId included"],
        [(scan[0:2], "companyId = 0xffff (uint16 LE), unregistered use", False),
         (scan[2:6], f"boardId = {BOARD_ID}, same bytes as in hello", False),
         (scan[6:7], f"protocolVersion = {PROTOCOL_VERSION}", False),
         (scan[7:8], f"freeSlots = {FREE_SLOTS}", False)]))

    # -- messages (§9) -------------------------------------------------------
    describe = {
        "boardId": BOARD_ID,
        "firmware": "1.0.0",
        "hardware": "esp32-wroom-32",
        "capabilities": ["leds", "routes", "photo"],
        "maxMessage": MAX_MESSAGE,
        "maxChunk": MAX_CHUNK,
        "maxRouteHolds": MAX_ROUTE_HOLDS,
        "maxBatch": MAX_BATCH,
    }
    settings = {
        "configured": True,
        "name": BOARD_NAME,
        "settingsRevision": SETTINGS_REVISION,
        "holdsRevision": HOLDS_REVISION,
        "ledCount": LED_COUNT,
        "roles": dict(ROLES),
        "power": {"budgetMa": 4000, "maPerChannel": 20},
        "angleAdjustable": False,
        "maxConnections": MAX_CONNECTIONS,
        "idleTimeoutS": 600,
        "ledsOffAfterS": 1800,
        "layout": {"size": len(current_layout), "sha256": sha256(current_layout)},
        "photo": {
            "preview": {"size": len(preview), "sha256": sha256(preview), "mime": MIME_PNG,
                        "width": PREVIEW_PX[0], "height": PREVIEW_PX[1]},
            "full": {"size": len(photo), "sha256": sha256(photo), "mime": MIME_PNG,
                     "width": PHOTO_PX[0], "height": PHOTO_PX[1]},
        },
    }
    setleds_payload = render_route(SETTINGS_REVISION, 42, ROUTE_42, layout_changed, settings)
    chunk_payload = struct.pack("<BI", TRANSFER_ID, 0) + photo[:CHUNK_SIZE]

    messages = {
        "0x01-hello.request.json": {"protocolVersion": PROTOCOL_VERSION, "client": "kb-app/0.3.1"},
        "0x01-hello.response.json": {"protocolVersion": PROTOCOL_VERSION, "boardId": BOARD_ID,
                                     "name": BOARD_NAME},
        "0x01-hello.unsupportedVersion.error.json": {"code": 102,
                                                     "protocolVersion": PROTOCOL_VERSION},
        "0x02-describe.response.json": describe,
        "0x03-getSettings.response.json": settings,
        "0x03-getSettings.unconfigured.response.json": {"configured": False,
                                                        "settingsRevision": 0},
        "0x04-getStatus.response.json": {
            "settingsRevision": SETTINGS_REVISION,
            "routesRevision": 87,
            "storageId": STORAGE_ID,
            "routeCount": 143,
            "storageFree": 1043968,
            "brightness": BRIGHTNESS,
            "wall": {"source": "app", "routeId": 42, "wallSeq": WALL_SEQ},
            "uptime": 90210,
        },
        "0x05-statusChanged.event.json": {"settingsRevision": 13, "routesRevision": 88,
                                          "storageId": STORAGE_ID},
        "0x06-disconnecting.event.json": {"reason": "idle"},
        "0x10-setLeds.response.json": {"limited": False, "wallSeq": WALL_SEQ},
        "0x10-setLeds.staleSettings.error.json": {"code": 203,
                                                  "message": "settingsRevision 11, expected 12"},
        "0x11-setBrightness.request.json": {"value": 120},
        "0x11-setBrightness.response.json": {"limited": False},
        "0x12-wallChanged.event.json": {"source": "app", "routeId": 42, "wallSeq": WALL_SEQ,
                                        "brightness": BRIGHTNESS},
        "0x20-syncIndex.request.json": {"since": 87},
        "0x20-syncIndex.response.json": {
            "routesRevision": 92,
            "reset": False,
            "entries": [index_entry(ROUTE_42), {"routeId": 17, "rev": 89, "deleted": True}],
            "more": True,
        },
        "0x20-syncIndex.reset.response.json": {
            "routesRevision": 92,
            "reset": True,
            "entries": [index_entry(ROUTE_3), index_entry(ROUTE_42), index_entry(ROUTE_58)],
            "more": False,
        },
        "0x21-getRoutes.request.json": {"routeIds": [42, 17, 103]},
        "0x21-getRoutes.response.json": {"routes": [ROUTE_42], "missing": [17, 103]},
        "0x22-saveRoute.request.json": {"token": 2718281828, "ownerId": "9c41e07ad2b35f18",
                                        "route": SAVE_ROUTE},
        "0x22-saveRoute.response.json": {"routeId": 144, "rev": 88},
        "0x22-saveRoute.invalidPosition.error.json": {
            "code": 201, "message": "holds[1]: c=12 r=3 is outside the grid"},
        "0x23-updateRoute.request.json": {"token": 1414213562, "routeId": 144, "baseRev": 88,
                                          "route": UPDATE_ROUTE},
        "0x23-updateRoute.response.json": {"rev": 92},
        "0x23-updateRoute.conflict.error.json": {"code": 205,
                                                 "message": "route 144 is at rev 90, baseRev 88"},
        "0x24-deleteRoute.request.json": {"token": 1732050807, "routeId": 144},
        "0x24-deleteRoute.response.json": {"rev": 93},
        "0x30-xferBegin.request.json": {"resource": "photo", "offset": 0,
                                        "chunkSize": CHUNK_SIZE},
        "0x30-xferBegin.response.json": {"transferId": TRANSFER_ID, "size": len(photo),
                                         "sha256": sha256(photo), "mime": MIME_PNG,
                                         "chunkSize": CHUNK_SIZE},
        "0x32-xferEnd.event.json": {"transferId": TRANSFER_ID},
        "0x33-xferAbort.event.json": {"transferId": TRANSFER_ID, "reason": "resourceChanged"},
    }
    for name, payload in messages.items():
        write(files, name, json_file(payload))

    write(files, "0x10-setLeds.request.bin", setleds_payload)
    write(files, "0x10-setLeds.request.hexdump.txt", hexdump(
        ["0x10-setLeds.request.bin: setLeds payload (PROTOCOL.md §9.7), "
         f"{len(setleds_payload)} bytes",
         "Route 42 of 0x21-getRoutes.response.json on resources/layout.holdChanges.json,",
         "colours from 0x03-getSettings.response.json. The foot hold c4 r10 has no LED."],
        setleds_fields(setleds_payload, layout_changed, settings)))
    write(files, "0x31-xferChunk.event.bin", chunk_payload)
    write(files, "0x31-xferChunk.event.hexdump.txt", hexdump(
        ["0x31-xferChunk.event.bin: xferChunk payload (PROTOCOL.md §6.3), "
         f"{len(chunk_payload)} bytes",
         "First chunk of resources/photo.png, transfer of 0x30-xferBegin.response.json"],
        chunk_fields(chunk_payload)))

    # -- errors/ (codes not in the §12 tree) ---------------------------------
    errors = [
        (0x22, 101, "incomplete message: 412 of 980 payload bytes, no packet for 1 s"),
        (0x50, 103, "unknown type 0x50"),
        (0x11, 104, "required field value missing"),
        (0x22, 105, "length 5120 above maxMessage 4096"),
        (0x24, 202, "route 144 does not exist"),
        (0x30, 204, "unknown resource thumbnail"),
        (0x30, 301, "transfer 3 is running on this connection"),
        (0x10, 302, "board not configured"),
        (0x22, 401, "no space left for this route"),
    ]
    for type_, code, message in errors:
        name = TYPE_NAMES.get(type_, "unknown")
        write(files, f"errors/0x{type_:02x}-{name}.{code}-{ERROR_NAMES[code]}.error.json",
              json_file({"code": code, "message": message}))

    # -- derived/route-to-frame ----------------------------------------------
    for name, (route_id, r, _) in ROUTE_TO_FRAME.items():
        case = {
            "layout": "resources/layout.holdChanges.json",
            "settings": "0x03-getSettings.response.json",
            "settingsRevision": SETTINGS_REVISION,
            "routeId": route_id,
            "route": r,
        }
        payload = render_route(case["settingsRevision"], route_id, r, layout_changed, settings)
        base = f"derived/route-to-frame/{name}"
        write(files, base + ".json", json_file(case))
        write(files, base + ".bin", payload)
        write(files, base + ".hexdump.txt", hexdump(
            [f"{name}.bin: expected setLeds payload (PROTOCOL.md §9.7) for {name}.json, "
             f"{len(payload)} bytes"],
            setleds_fields(payload, layout_changed, settings)))

    # -- derived/route-warnings ----------------------------------------------
    for name, (layout_name, r, outdated, missing) in ROUTE_WARNINGS.items():
        expected = route_warnings(r, layouts[layout_name])
        case = {"layout": f"resources/{layout_name}", "route": r, "expected": expected}
        write(files, f"derived/route-warnings/{name}.json", json_file(case))

    # -- frames/ -------------------------------------------------------------
    frames = [
        ("0x01-hello.request", 0x01, 1, "request",
         json_wire(messages["0x01-hello.request.json"]), None),
        ("0x10-setLeds.request", 0x10, 5, "request", setleds_payload,
         lambda p: setleds_fields(p, layout_changed, settings)),
        ("0x03-getSettings.response", 0x03, 3, "response",
         json_wire(messages["0x03-getSettings.response.json"]), None),
        ("0x31-xferChunk.event", 0x31, 0, "event", chunk_payload, chunk_fields),
    ]
    for name, type_, id_, kind, payload, payload_fields in frames:
        binary = payload_fields is not None
        head = header(type_, id_, kind, binary, len(payload))
        message = head + payload
        base = f"frames/{name}"
        write(files, f"{base}/message.bin", message)
        title = [f"{name}/message.bin: complete message incl. 5-byte header "
                 f"(PROTOCOL.md §4), {len(message)} bytes"]
        for mtu in FRAME_MTUS:
            size = mtu - 3
            packets = [message[o:o + size] for o in range(0, len(message), size)]
            for n, packet in enumerate(packets):
                write(files, f"{base}/mtu{mtu}/{n:03}.bin", packet)
            title.append(f"MTU {mtu}: {size} bytes per packet, {len(packets)} packet(s) "
                         f"mtu{mtu}/000.bin to {len(packets) - 1:03}.bin")
        flags = head[2]
        fields = [
            (head[0:1], f"type = 0x{type_:02x} {TYPE_NAMES[type_]}", False),
            (head[1:2], f"id = {id_}", False),
            (head[2:3], f"flags = 0x{flags:02x}: payload {'binary' if binary else 'JSON'}, "
                        f"kind {kind}", False),
            (head[3:5], f"length = {len(payload)} (uint16 LE)", False),
        ]
        if binary:
            fields += payload_fields(payload)
        else:
            fields.append((payload, "payload, JSON (UTF-8)", True))
        write(files, f"{base}/message.hexdump.txt", hexdump(title, fields, FRAME_MTUS))

    # -- grades.json (Appendix A) --------------------------------------------
    write(files, "grades.json", json_file({
        "font": FONT_GRADES,
        "v": [{"v": v, "shownFor": shown, "stored": stored} for v, shown, stored in V_GRADES],
    }))

    return files


# ---------------------------------------------------------------------------
# Checks: everything is read back from disk
# ---------------------------------------------------------------------------

TREE_12 = """
scan/scanResponse.bin scan/scanResponse.hexdump.txt
0x01-hello.request.json 0x01-hello.response.json 0x01-hello.unsupportedVersion.error.json
0x02-describe.response.json 0x03-getSettings.response.json
0x03-getSettings.unconfigured.response.json 0x04-getStatus.response.json
0x05-statusChanged.event.json 0x06-disconnecting.event.json 0x10-setLeds.request.bin
0x10-setLeds.request.hexdump.txt 0x10-setLeds.response.json
0x10-setLeds.staleSettings.error.json 0x11-setBrightness.request.json
0x11-setBrightness.response.json 0x12-wallChanged.event.json 0x20-syncIndex.request.json
0x20-syncIndex.response.json 0x20-syncIndex.reset.response.json
0x21-getRoutes.request.json 0x21-getRoutes.response.json 0x22-saveRoute.request.json
0x22-saveRoute.response.json 0x22-saveRoute.invalidPosition.error.json
0x23-updateRoute.request.json 0x23-updateRoute.response.json
0x23-updateRoute.conflict.error.json 0x24-deleteRoute.request.json
0x24-deleteRoute.response.json 0x30-xferBegin.request.json 0x30-xferBegin.response.json
0x31-xferChunk.event.bin 0x31-xferChunk.event.hexdump.txt 0x32-xferEnd.event.json
0x33-xferAbort.event.json resources/layout.json resources/layout.holdChanges.json
grades.json
""".split()

ROUTE_KEYS = ["routeId", "rev", "name", "grade", "setter", "description", "createdAt",
              "updatedAt", "holdsRevision", "angle", "feet", "tags", "holds"]


class Checker:
    def __init__(self):
        self.results = []

    def check(self, name, ok, detail=""):
        self.results.append((name, bool(ok), detail))

    def failed(self):
        return [r for r in self.results if not r[1]]


def utf8_bytes(s):
    return len(s.encode("utf-8"))


def control_chars(s):
    return {ch for ch in s if ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F}


def u_escapes_ok(data):
    """§3: \\u escapes only for control characters (an escaped backslash is no escape)."""
    for m in re.finditer(rb"(\\+)u([0-9a-fA-F]{4})", data):
        if len(m[1]) % 2 == 1 and not control_chars(chr(int(m[2], 16))):
            return False
    return True


def route_problems(r, layout, board_route, allow_missing=()):
    """Limits of §10.1. board_route: routeId and rev present (else both absent)."""
    p = []
    keys = list(r)
    if [k for k in ROUTE_KEYS if k in r] != keys:
        p.append(f"unknown keys or wrong order: {keys}")
    if board_route != ("routeId" in r and "rev" in r) or ("routeId" in r) != ("rev" in r):
        p.append("routeId/rev presence")
    if "routeId" in r and not 1 <= r["routeId"] <= 0xFFFFFFFF:
        p.append("routeId range")
    if not 1 <= utf8_bytes(r["name"]) <= 48:
        p.append("name length")
    if "grade" in r and r["grade"] not in FONT_GRADES:
        p.append("grade")
    if "setter" in r and utf8_bytes(r["setter"]) > 32:
        p.append("setter length")
    if "description" in r and utf8_bytes(r["description"]) > 280:
        p.append("description length")
    if not (isinstance(r["createdAt"], int) and isinstance(r["updatedAt"], int)
            and 0 < r["createdAt"] <= r["updatedAt"]):
        p.append("createdAt/updatedAt")
    if not 0 <= r["holdsRevision"] <= HOLDS_REVISION:
        p.append("holdsRevision above the board's value")
    if "angle" in r:
        p.append("angle present, but angleAdjustable is false")
    if "feet" in r and r["feet"] not in ("marked", "kicker", "any"):
        p.append("feet")
    if "tags" in r and (len(r["tags"]) > 8 or any(utf8_bytes(t) > 24 for t in r["tags"])):
        p.append("tags")
    texts = [r["name"], r.get("setter", "")] + r.get("tags", [])
    if any(control_chars(t) for t in texts) or control_chars(r.get("description", "")) - {"\n"}:
        p.append("control character in a text field")
    holds = r["holds"]
    if not 1 <= len(holds) <= MAX_ROUTE_HOLDS:
        p.append("hold count")
    seen = set()
    for h in holds:
        if list(h) != ["c", "r", "role"]:
            p.append(f"hold keys {list(h)}")
        if h["role"] not in ROLES:
            p.append(f"role {h['role']}")
        pos = (h["c"], h["r"])
        if pos in seen:
            p.append(f"position {pos} twice")
        seen.add(pos)
        if pos in allow_missing:
            continue
        i = grid_index(layout, *pos)
        if i is None or layout["positions"][i] is None:
            p.append(f"position {pos} has no hold (§5.2)")
    return p


def walk(obj, path=""):
    yield path, obj
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for n, v in enumerate(obj):
            yield from walk(v, f"{path}[{n}]")


def png_info(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, chunks = 8, []
    while pos < len(data):
        length, kind = struct.unpack_from(">I4s", data, pos)
        body = data[pos + 8:pos + 8 + length]
        crc, = struct.unpack_from(">I", data, pos + 8 + length)
        assert crc == zlib.crc32(kind + body), f"CRC of {kind}"
        chunks.append((kind, body))
        pos += 12 + length
    assert [k for k, _ in chunks] == [b"IHDR", b"IDAT", b"IEND"]
    w, h, depth, colour, comp, filt, interlace = struct.unpack(">IIBBBBB", chunks[0][1])
    assert (depth, colour, comp, filt, interlace) == (8, 2, 0, 0, 0)
    raw = zlib.decompress(chunks[1][1])
    assert len(raw) == h * (1 + 3 * w)
    rows = {raw[y * (1 + 3 * w):(y + 1) * (1 + 3 * w)] for y in range(h)}
    assert rows == {b"\x00" + bytes(PHOTO_RGB) * w}, "not one colour"
    return w, h


def run_checks(files):
    ck = Checker()
    disk = {p.relative_to(FIXTURES).as_posix(): p.read_bytes()
            for p in sorted(FIXTURES.rglob("*")) if p.is_file() and p.name not in KEEP}
    ck.check("files on disk == files generated", disk == files, f"{len(disk)} files")

    def load(rel):
        return json.loads(disk[rel])

    missing_tree = [f for f in TREE_12 if f not in disk]
    ck.check("every file of the §12 tree exists", not missing_tree, ", ".join(missing_tree))

    # -- format of text files --
    bad = []
    for rel, data in disk.items():
        if not rel.endswith((".json", ".txt")):
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            bad.append(rel + " (not UTF-8)")
            continue
        if data.startswith(b"\xef\xbb\xbf") or b"\r" in data or not data.endswith(b"\n") \
                or data.endswith(b"\n\n"):
            bad.append(rel + " (BOM, CR or final newline)")
        if rel.endswith(".json"):
            obj = json.loads(text)
            canonical = layout_file(obj) if rel.startswith("resources/layout") else json_file(obj)
            if data != canonical:
                bad.append(rel + " (not in canonical 2-space form)")
            if not u_escapes_ok(data):
                bad.append(rel + " (\\u escape for a character that is no control character)")
            for path, v in walk(obj):
                if isinstance(v, float):
                    bad.append(f"{rel}{path} (float)")
                if v is None and not rel.startswith("resources/"):
                    bad.append(f"{rel}{path} (null)")
    ck.check("text files: UTF-8 without BOM, LF, one final newline, 2-space JSON, "
             "\\u escapes only for control characters (§3), no floats, null only in layouts",
             not bad, "; ".join(bad))

    # -- hex dumps round-trip --
    dumps = [rel for rel in disk if rel.endswith(".hexdump.txt")]
    bad = []
    for rel in dumps:
        target = rel.replace(".hexdump.txt", ".bin")
        if parse_hexdump(disk[rel]) != disk[target]:
            bad.append(rel)
    ck.check("every hexdump parses back to its .bin", not bad and dumps,
             f"{len(dumps)} hexdumps; " + ", ".join(bad))
    bins = [rel for rel in disk if rel.endswith(".bin") and "/mtu" not in rel]
    no_dump = [b for b in bins if b.replace(".bin", ".hexdump.txt") not in disk]
    ck.check("every binary fixture (except packets) has a hexdump", not no_dump,
             ", ".join(no_dump))

    # -- resources --
    lay = load("resources/layout.json")
    lay_c = load("resources/layout.holdChanges.json")
    lay_r = load("resources/layout.holdRemoved.json")
    n = lay["cols"] * lay["rows"]
    ck.check("layout: 12 x 12, face 1800 x 2200, 144 entries per array",
             (lay["cols"], lay["rows"], lay["face"]) == (12, 12, {"w": 1800, "h": 2200})
             and all(len(lay[k]) == n for k in ("positions", "ledMap", "holdChanges")))
    ck.check("layout: positions match the §5.1 example ([417,1364], [1250,1364]) "
             "and lie in 0..10000",
             lay["positions"][0] == [417, 1364] and lay["positions"][1] == [1250, 1364]
             and all(0 <= v <= 10000 for p in lay["positions"] for v in p))
    ck.check("layout: every position has a hold, rows 9-11 without LED",
             all(p is not None for p in lay["positions"])
             and all(lay["ledMap"][i] is None for i in range(9 * 12, n))
             and all(lay["ledMap"][i] is not None for i in range(9 * 12)))
    ck.check("layout: ledMap is a snake over 0..107 (ledMap[11]=11, [12]=23, [13]=22)",
             sorted(v for v in lay["ledMap"] if v is not None) == list(range(108))
             and lay["ledMap"][11] == 11 and lay["ledMap"][12] == 23 and lay["ledMap"][13] == 22)
    ck.check("layout.json: holdChanges all 0 (holdsRevision 0)",
             set(lay["holdChanges"]) == {0})
    ck.check("layout.holdChanges.json: same wall, max holdChanges = holdsRevision 5, "
             "one value between 0 and 5",
             {k: v for k, v in lay_c.items() if k != "holdChanges"}
             == {k: v for k, v in lay.items() if k != "holdChanges"}
             and max(lay_c["holdChanges"]) == HOLDS_REVISION
             and any(0 < v < HOLDS_REVISION for v in lay_c["holdChanges"]))
    ri = grid_index(lay, *REMOVED_HOLD)
    diff = [i for i in range(n) if (lay_r["positions"][i], lay_r["ledMap"][i])
            != (lay_c["positions"][i], lay_c["ledMap"][i])]
    ck.check("layout.holdRemoved.json: only (6,2) differs, positions and ledMap null there, "
             "LED 30 unused, holdChanges unchanged",
             diff == [ri] and lay_r["positions"][ri] is None and lay_r["ledMap"][ri] is None
             and lay_c["ledMap"][ri] == 30 and 30 not in lay_r["ledMap"]
             and lay_r["holdChanges"] == lay_c["holdChanges"] and lay_c["positions"][ri] is not None)
    try:
        dims = (png_info(disk["resources/photoPreview.png"]), png_info(disk["resources/photo.png"]))
        ck.check("PNGs valid (signature, CRCs, IHDR, IDAT inflates), one colour, 32x39 and 160x196",
                 dims == (PREVIEW_PX, PHOTO_PX), str(dims))
    except AssertionError as e:
        ck.check("PNGs valid", False, str(e))

    settings = load("0x03-getSettings.response.json")
    photo = disk["resources/photo.png"]
    preview = disk["resources/photoPreview.png"]
    current = disk["resources/layout.holdChanges.json"]

    def meta(data, w, h):
        return {"size": len(data), "sha256": sha256(data), "mime": MIME_PNG, "width": w, "height": h}

    ck.check("getSettings: layout size/sha256 = layout.holdChanges.json, photo values real",
             settings["layout"] == {"size": len(current), "sha256": sha256(current)}
             and settings["photo"]["preview"] == meta(preview, *PREVIEW_PX)
             and settings["photo"]["full"] == meta(photo, *PHOTO_PX))
    xb_req = load("0x30-xferBegin.request.json")
    xb = load("0x30-xferBegin.response.json")
    ck.check("xferBegin: size/sha256/mime = resources/photo.png, chunkSize = min(maxChunk, 247-13) = 234",
             xb_req["resource"] == "photo" and xb["size"] == len(photo)
             and xb["sha256"] == sha256(photo) and xb["mime"] == MIME_PNG
             and xb["chunkSize"] == xb_req["chunkSize"] == 234 == min(MAX_CHUNK, 247 - 13))
    chunk = disk["0x31-xferChunk.event.bin"]
    tid, off = struct.unpack_from("<BI", chunk)
    ck.check("xferChunk: transferId from xferBegin, offset 0, data = photo[0:chunkSize]",
             tid == xb["transferId"] and off == 0 and chunk[5:] == photo[:xb["chunkSize"]]
             and len(chunk) - 5 == xb["chunkSize"] < len(photo))
    ck.check("xferEnd/xferAbort carry the same transferId",
             load("0x32-xferEnd.event.json")["transferId"] == xb["transferId"]
             == load("0x33-xferAbort.event.json")["transferId"])

    # -- scan --
    scan = disk["scan/scanResponse.bin"]
    hello = load("0x01-hello.response.json")
    ck.check("scanResponse.bin: 8 bytes, companyId 0xffff LE, boardId as in hello, version 1, "
             "freeSlots <= maxConnections",
             len(scan) == 8 and scan[0:2] == b"\xff\xff" and scan[2:6].hex() == hello["boardId"]
             and scan[6] == PROTOCOL_VERSION and scan[7] <= settings["maxConnections"],
             scan.hex(" "))

    # -- setLeds payloads --
    frame_len = 7 + 3 * settings["ledCount"]
    setleds = {rel: data for rel, data in disk.items()
               if rel == "0x10-setLeds.request.bin" or rel.startswith("derived/route-to-frame/")
               and rel.endswith(".bin")}
    setleds["frames/0x10-setLeds.request/message.bin[5:]"] = \
        disk["frames/0x10-setLeds.request/message.bin"][5:]
    ck.check(f"every setLeds payload is exactly 7 + 3 x 108 = {frame_len} bytes",
             frame_len == 331 and all(len(d) == frame_len for d in setleds.values()),
             f"{len(setleds)} payloads")
    ck.check("setLeds request = derived/route-to-frame/holdWithoutLed.bin (route 42)",
             disk["0x10-setLeds.request.bin"] == disk["derived/route-to-frame/holdWithoutLed.bin"])

    # -- route-to-frame: independent decoding back to the route --
    bad = []
    colours = {bytes.fromhex(c[1:]): role for role, c in settings["roles"].items()}
    for name, (route_id, _, expected_lit) in ROUTE_TO_FRAME.items():
        case = load(f"derived/route-to-frame/{name}.json")
        data = disk[f"derived/route-to-frame/{name}.bin"]
        if list(case) != ["layout", "settings", "settingsRevision", "routeId", "route"]:
            bad.append(f"{name}: keys")
        layout = load(case["layout"])
        s = load(case["settings"])
        rev, reserved, rid = struct.unpack_from("<HBI", data)
        if (rev, reserved, rid) != (case["settingsRevision"], 0, case["routeId"]) \
                or rev != s["settingsRevision"]:
            bad.append(f"{name}: head")
        r = case["route"]
        if case["routeId"] == 0 and "routeId" in r or case["routeId"] and r.get("routeId") != case["routeId"]:
            bad.append(f"{name}: routeId of route")
        pos_of_led = {led: (i % layout["cols"], i // layout["cols"])
                      for i, led in enumerate(layout["ledMap"]) if led is not None}
        role_at = {(h["c"], h["r"]): h["role"] for h in r["holds"]}
        lit = {}
        for led in range(s["ledCount"]):
            rgb = data[7 + 3 * led:10 + 3 * led]
            if rgb == b"\0\0\0":
                continue
            pos = pos_of_led.get(led)
            if pos not in role_at or colours.get(rgb) != role_at[pos]:
                bad.append(f"{name}: led {led}")
            lit[led] = colours.get(rgb)
        with_led = [p for p in role_at
                    if grid_index(layout, *p) is not None
                    and layout["ledMap"][grid_index(layout, *p)] is not None]
        if len(lit) != len(with_led) or lit != expected_lit:
            bad.append(f"{name}: lit LEDs {lit}")
    ck.check("route-to-frame: every lit LED decodes to a route hold in its role colour, "
             "every hold with LED is lit, matches the hand-written expectation",
             not bad, "; ".join(bad))
    ck.check("route-to-frame: onlyHoldsWithoutLeds is all zeros after the 7-byte head",
             set(disk["derived/route-to-frame/onlyHoldsWithoutLeds.bin"][7:]) == {0})
    ends = disk["derived/route-to-frame/chainEnds.bin"]
    ck.check("route-to-frame: chainEnds lights LED 0 and LED 107",
             ends[7:10] != b"\0\0\0" and ends[7 + 3 * 107:] != b"\0\0\0")
    ck.check("route-to-frame: routeIdNonZero carries 0x12345678 as 78 56 34 12",
             disk["derived/route-to-frame/routeIdNonZero.bin"][3:7] == bytes.fromhex("78563412"))
    ck.check("route-to-frame: routeIdZero carries routeId 0 and a route without routeId/rev",
             disk["derived/route-to-frame/routeIdZero.bin"][3:7] == b"\0\0\0\0"
             and "routeId" not in load("derived/route-to-frame/routeIdZero.json")["route"])
    roles_used = {h["role"] for h in load("derived/route-to-frame/allRoles.json")["route"]["holds"]}
    ck.check("route-to-frame: allRoles uses all four roles, each with an LED",
             roles_used == set(ROLES)
             and set(ROUTE_TO_FRAME["allRoles"][2].values()) == set(ROLES))

    # -- route-warnings --
    bad = []
    for name, (layout_name, _, outdated, missing) in ROUTE_WARNINGS.items():
        case = load(f"derived/route-warnings/{name}.json")
        if list(case) != ["layout", "route", "expected"] \
                or list(case["expected"]) != ["outdated", "missing"]:
            bad.append(f"{name}: keys")
        exp = case["expected"]
        want = {"outdated": [{"c": c, "r": r} for c, r in outdated],
                "missing": [{"c": c, "r": r} for c, r in missing]}
        if exp != want:
            bad.append(f"{name}: expected {exp} != hand-written {want}")
        if route_warnings(case["route"], load(case["layout"])) != exp:
            bad.append(f"{name}: recomputed")
        for key in ("outdated", "missing"):
            if exp[key] != sorted(exp[key], key=lambda p: (p["r"], p["c"])):
                bad.append(f"{name}: {key} not in reading order")
        if {(p["c"], p["r"]) for p in exp["outdated"]} & {(p["c"], p["r"]) for p in exp["missing"]}:
            bad.append(f"{name}: position in both lists")
    ck.check("route-warnings: expectation = hand-written = recomputed, reading order, "
             "no position in both lists", not bad, "; ".join(bad))
    oam = load("derived/route-warnings/outdatedAndMissing.json")
    lr = load(oam["layout"])
    ck.check("route-warnings: outdatedAndMissing has a missing hold whose holdChanges is above "
             "route.holdsRevision (so it would be outdated if it were not missing)",
             any(lr["holdChanges"][grid_index(lr, p["c"], p["r"])] > oam["route"]["holdsRevision"]
                 for p in oam["expected"]["missing"] if grid_index(lr, p["c"], p["r"]) is not None))
    neq = load("derived/route-warnings/notOutdatedEqualRevision.json")
    ln = load(neq["layout"])
    ck.check("route-warnings: notOutdatedEqualRevision has a hold with holdChanges == "
             "route.holdsRevision > 0",
             any(ln["holdChanges"][grid_index(ln, h["c"], h["r"])] == neq["route"]["holdsRevision"] > 0
                 for h in neq["route"]["holds"]))
    ck.check("route-warnings: missingOutsideGrid uses c = 12",
             load("derived/route-warnings/missingOutsideGrid.json")["expected"]["missing"]
             == [{"c": 12, "r": 4}])

    # -- routes: limits of §10.1 --
    bad = []
    routes = [("getRoutes", load("0x21-getRoutes.response.json")["routes"][0], True, (), lay_c),
              ("saveRoute", load("0x22-saveRoute.request.json")["route"], False, (), lay_c),
              ("updateRoute", load("0x23-updateRoute.request.json")["route"], False, (), lay_c)]
    for name in ROUTE_TO_FRAME:
        case = load(f"derived/route-to-frame/{name}.json")
        routes.append((f"route-to-frame/{name}", case["route"], case["routeId"] != 0, (),
                       load(case["layout"])))
    for name in ROUTE_WARNINGS:
        case = load(f"derived/route-warnings/{name}.json")
        allow = {(p["c"], p["r"]) for p in case["expected"]["missing"]}
        routes.append((f"route-warnings/{name}", case["route"], True, allow, load(case["layout"])))
    for name, r, board_route, allow, layout in routes:
        problems = route_problems(r, layout, board_route, allow)
        if problems:
            bad.append(f"{name}: {problems}")
    ck.check(f"all {len(routes)} routes keep the limits of §10.1 (positions per §5.2 except the "
             "expected missing holds of route-warnings), no angle", not bad, "; ".join(bad))

    # -- route index --
    sync = load("0x20-syncIndex.response.json")
    since = load("0x20-syncIndex.request.json")["since"]
    reset = load("0x20-syncIndex.reset.response.json")
    by_id = {r["routeId"]: r for r in load("0x21-getRoutes.response.json")["routes"]}
    bad = []
    for label, resp in (("response", sync), ("reset", reset)):
        revs = [e["rev"] for e in resp["entries"]]
        if revs != sorted(revs) or len(set(revs)) != len(revs) or max(revs) > resp["routesRevision"]:
            bad.append(f"{label}: revs {revs}")
        if resp["more"] and revs[-1] >= resp["routesRevision"]:
            bad.append(f"{label}: more without anything left")
        for e in resp["entries"]:
            if e.get("deleted"):
                if list(e) != ["routeId", "rev", "deleted"] or e["deleted"] is not True:
                    bad.append(f"{label}: tombstone {e}")
                continue
            expected_keys = [k for k in ROUTE_KEYS if k in e and k not in ("description", "holds")]
            if list(e) != expected_keys + ["holdCount"] or "angle" in e:
                bad.append(f"{label}: entry keys {list(e)}")
            if e["routeId"] in by_id and e != index_entry(by_id[e["routeId"]]):
                bad.append(f"{label}: entry {e['routeId']} differs from getRoutes")
    if not (sync["reset"] is False and sync["more"] is True
            and all(e["rev"] > since for e in sync["entries"])
            and any(e.get("deleted") for e in sync["entries"])):
        bad.append("response: reset/more/since/tombstone")
    if not (reset["reset"] is True and not any(e.get("deleted") for e in reset["entries"])):
        bad.append("reset: flags or tombstones")
    ck.check("syncIndex: sorted by rev, rev > since, tombstone and more = true, reset without "
             "tombstones, index entries = §10.2 of the full route", not bad, "; ".join(bad))
    gr_req = load("0x21-getRoutes.request.json")["routeIds"]
    gr = load("0x21-getRoutes.response.json")
    ck.check("getRoutes: at most maxBatch IDs, routes + missing = requested IDs",
             len(gr_req) <= MAX_BATCH
             and sorted([r["routeId"] for r in gr["routes"]] + gr["missing"]) == sorted(gr_req))

    # -- wall state --
    status = load("0x04-getStatus.response.json")
    sl = load("0x10-setLeds.response.json")
    wc = load("0x12-wallChanged.event.json")
    sl_rid = struct.unpack_from("<I", disk["0x10-setLeds.request.bin"], 3)[0]
    ck.check("wallSeq in getStatus.wall, setLeds response and wallChanged, uint32, one "
             "consistent wall (route 42, wallSeq 3108, brightness 160)",
             status["wall"]["wallSeq"] == sl["wallSeq"] == wc["wallSeq"] == WALL_SEQ < 2 ** 31
             and status["wall"]["routeId"] == wc["routeId"] == sl_rid == 42
             and status["brightness"] == wc["brightness"]
             and status["settingsRevision"] == settings["settingsRevision"]
             == struct.unpack_from("<H", disk["0x10-setLeds.request.bin"])[0])

    # -- errors --
    pattern = re.compile(r"^errors/0x([0-9a-f]{2})-([A-Za-z]+)\.(\d{3})-([A-Za-z]+)\.error\.json$")
    bad, codes = [], set()
    for rel in (r for r in disk if r.startswith("errors/")):
        m = pattern.match(rel)
        if not m:
            bad.append(rel)
            continue
        type_, tname, code, cname = int(m[1], 16), m[2], int(m[3]), m[4]
        body = load(rel)
        if body.get("code") != code or ERROR_NAMES.get(code) != cname \
                or TYPE_NAMES.get(type_, "unknown") != tname:
            bad.append(rel)
        codes.add(code)
    tree_codes = {load(rel)["code"] for rel in disk if "/" not in rel and rel.endswith(".error.json")}
    ck.check("errors/: exactly 101 103 104 105 202 204 301 302 401, file name matches type and code",
             not bad and codes == {101, 103, 104, 105, 202, 204, 301, 302, 401},
             ", ".join(bad) + f" codes {sorted(codes)}")
    ck.check("errors/ + §12 tree cover every V1 code except 206, without duplicates",
             codes | tree_codes == set(ERROR_NAMES) - {206} and not codes & tree_codes,
             f"tree {sorted(tree_codes)}")
    ck.check("102 carries protocolVersion",
             load("0x01-hello.unsupportedVersion.error.json") == {"code": 102, "protocolVersion": 1})

    # -- frames --
    bad = []
    expected_frames = {
        "0x01-hello.request": (0x01, "request", "0x01-hello.request.json"),
        "0x10-setLeds.request": (0x10, "request", "0x10-setLeds.request.bin"),
        "0x03-getSettings.response": (0x03, "response", "0x03-getSettings.response.json"),
        "0x31-xferChunk.event": (0x31, "event", "0x31-xferChunk.event.bin"),
    }
    counts = {}
    for name, (type_, kind, source) in expected_frames.items():
        msg = disk[f"frames/{name}/message.bin"]
        t, id_, flags, length = struct.unpack_from("<BBBH", msg)
        payload = msg[5:]
        binary = source.endswith(".bin")
        if (t, flags & 1, (flags >> 1) & 3, flags >> 3) != (type_, int(binary), KINDS[kind], 0) \
                or length != len(payload):
            bad.append(f"{name}: header")
        if kind == "event" and id_ != 0:
            bad.append(f"{name}: event id")
        if binary:
            if payload != disk[source]:
                bad.append(f"{name}: payload")
        elif json.loads(payload) != load(source) or payload != json_wire(load(source)):
            bad.append(f"{name}: payload not the compact form of {source}")
        for mtu in FRAME_MTUS:
            size = mtu - 3
            packets = [disk[rel] for rel in sorted(disk)
                       if rel.startswith(f"frames/{name}/mtu{mtu}/")]
            names = sorted(rel.rsplit("/", 1)[1] for rel in disk
                           if rel.startswith(f"frames/{name}/mtu{mtu}/"))
            if b"".join(packets) != msg or names != [f"{i:03}.bin" for i in range(len(packets))] \
                    or any(len(p) != size for p in packets[:-1]) or not 0 < len(packets[-1]) <= size:
                bad.append(f"{name}: mtu{mtu} packets")
            counts[f"{name} mtu{mtu}"] = len(packets)
    ck.check("frames: header (type, flags, id, length) right, payload = fixture (JSON compact), "
             "packets concatenate to message.bin, all full except the last",
             not bad, "; ".join(bad))
    ck.check("frames: getSettings response needs several packets at MTU 247",
             counts["0x03-getSettings.response mtu247"] > 1,
             ", ".join(f"{k}: {v}" for k, v in counts.items()))
    ck.check("frames: xferChunk message fills exactly one MTU 247 packet (5 + 5 + 234 = 244)",
             len(disk["frames/0x31-xferChunk.event/message.bin"]) == 244)

    # -- sizes against maxMessage --
    requests = {rel: disk[rel] if rel.endswith(".bin") else json_wire(load(rel))
                for rel in disk if "/" not in rel and ".request." in rel and not rel.endswith(".txt")}
    too_big = [rel for rel, d in requests.items() if len(d) > MAX_MESSAGE]
    ck.check("every request payload <= maxMessage 4096", not too_big,
             f"largest {max(len(d) for d in requests.values())} bytes")
    for mixed in (False, True):
        r = largest_route(mixed)
        problems = [p for p in route_problems(r, lay_c, False)
                    if p not in ("holdsRevision above the board's value",
                                 "angle present, but angleAdjustable is false")]
        text = "multi-byte characters, quotes, backslashes and line feeds" if mixed \
            else "only characters that JSON encodes as 2 bytes (quote, line feed)"
        save = len(json_wire({"token": INT_MAX, "ownerId": "f" * 16, "route": r}))
        update = len(json_wire({"token": INT_MAX, "routeId": INT_MAX, "baseRev": INT_MAX,
                                "route": r}))
        ck.check(f"saveRoute and updateRoute with {MAX_ROUTE_HOLDS} holds and every field at its "
                 f"limit, text of {text}, fit into maxMessage {MAX_MESSAGE} (§9.2)",
                 not problems and max(save, update) <= MAX_MESSAGE,
                 f"saveRoute {save} bytes, updateRoute {update} bytes"
                 + (f"; {problems}" if problems else ""))

    # -- grades --
    grades = load("grades.json")
    shown = [g for v in grades["v"] for g in v["shownFor"]]
    ck.check("grades.json: font = Appendix A, every Font grade shown as exactly one V, "
             "stored is one of shownFor",
             grades["font"] == FONT_GRADES and sorted(shown, key=FONT_GRADES.index) == FONT_GRADES
             and shown == FONT_GRADES
             and all(v["stored"] in v["shownFor"] for v in grades["v"])
             and [v["v"] for v in grades["v"]] == [f"V{i}" for i in range(18)])

    return ck


INT_MAX = 0xFFFFFFFF    # integers without a stated limit are taken at the uint32 maximum


def fill(limit, sample=""):
    """sample, then quotes up to exactly `limit` UTF-8 bytes."""
    s = sample + '"' * (limit - utf8_bytes(sample))
    assert utf8_bytes(s) == limit
    return s


def largest_route(mixed):
    """A route with every field of §10.1 at its limit, on the reference wall.

    Raw UTF-8 costs 1 byte per byte on the wire (§3); quote, backslash and line
    feed cost 2; control characters are not allowed (§10.1). So text made only
    of 2-byte characters (mixed=False) gives the largest possible message.
    mixed=True mixes in multi-byte characters, backslashes and line feeds."""
    digits = lambda p: len(str(p[0])) + len(str(p[1]))
    cells = sorted(((c, r) for r in range(ROWS) for c in range(COLS)), key=lambda p: -digits(p))
    role = max(ROLES, key=len)
    if mixed:
        name = fill(48, "Grüne Wand 😀 \\ ")
        setter = fill(32, "Jörg ✓ \\ ")
        description = fill(280, "Line one: ü, 😀, \\.\nLine two \"quoted\".\n")
        tags = [fill(24, f"tag{n} ö😀\\") for n in range(8)]
    else:
        name, setter, tags = fill(48), fill(32), [fill(24)] * 8
        description = '"\n' * 140
    assert utf8_bytes(description) == 280
    r = route(name, [(c, row, role) for c, row in cells[:MAX_ROUTE_HOLDS]],
              INT_MAX, INT_MAX, INT_MAX, grade=max(FONT_GRADES, key=len), setter=setter,
              description=description, feet=max(("marked", "kicker", "any"), key=len),
              tags=tags)
    r["angle"] = 90
    return {k: r[k] for k in ROUTE_KEYS if k in r}


def main():
    clean()
    files = generate()
    ck = run_checks(files)
    for name, ok, detail in ck.results:
        line = f"{'ok  ' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else "")
        print(line.replace("§", "section "))
    failed = ck.failed()
    print(f"\n{len(files)} files written, {len(ck.results) - len(failed)}/{len(ck.results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
