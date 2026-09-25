# Changelog

Every change to `PROTOCOL.md` gets an entry here (PROTOCOL.md §12). Entries are
document versions. Each entry states the `protocolVersion`, which changes only
by the rules in PROTOCOL.md §7.1.

## 1.0 · 2026-09-21 · protocolVersion 1

First version.

## 1.1 · 2026-09-25 · protocolVersion 1

Fixtures added, plus clarifications in `PROTOCOL.md`. Nothing changes on the
wire.

* `fixtures/`: every file of the §12 tree, plus:
  * `errors/`, one example per V1 error code not covered by the tree;
  * `resources/layout.holdRemoved.json`;
  * the photo resources;
  * `fixtures/README.md`, which describes every fixture family.
* `tools/generate_fixtures.py` generates and checks all fixtures (Python 3,
  standard library only).
* `.gitattributes`: LF for text, `*.bin` binary. `.gitignore`: `__pycache__/`.

Clarifications in `PROTOCOL.md`:

* §3: strings are sent as raw UTF-8; `\u` escapes are used only for control
  characters.
* §10.1: route text fields contain no control characters, except line feed in
  `description`.
* §12: the fixture tree shows the actual state, including `errors/`,
  `layout.holdRemoved.json`, the photos, `fixtures/README.md` and `tools/`.
  New: examples in this document are illustrative; the files in `fixtures/`
  are consistent with each other and are the reference for tests.
* The fixtures therefore use their own values instead of the example values:
  one board with one timeline of its route storage (`fixtures/README.md`,
  3.1). The script checks this.
* Appendix B: the row on `fixtures/` is removed. The `maxMessage` row now gives
  the size of the largest possible route message, about 3 350 bytes.
