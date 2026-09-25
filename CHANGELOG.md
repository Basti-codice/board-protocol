# Changelog

Every change to `PROTOCOL.md` gets an entry here (PROTOCOL.md §12). Entries are
document versions. Each entry states the `protocolVersion`, which changes only
by the rules in PROTOCOL.md §7.1.

## 1.0 · 2026-09-21 · protocolVersion 1

First version.

## 1.1 · 2026-09-25 · protocolVersion 1

Fixtures added. `PROTOCOL.md` is unchanged.

* `fixtures/`: every file of the §12 tree, plus:
  * `errors/`, one example per V1 error code not covered by the tree;
  * `resources/layout.holdRemoved.json`;
  * the photo resources;
  * `fixtures/README.md`, which describes every fixture family.
* `tools/generate_fixtures.py` generates and checks all fixtures (Python 3,
  standard library only).
* `.gitattributes`: LF for text, `*.bin` binary.
