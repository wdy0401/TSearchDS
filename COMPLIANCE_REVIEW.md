# Current release review — 2026-10-06

Project review, not a legal opinion or compliance certification.

The default interface is now Tkinter/ttk. Tk behavior tests cover concurrent
independent tabs, result sorting/filtering, copying, column/source display,
language switching, preference persistence and query privacy. The default
packaging recipe excludes PyQt, PySide and Qt. Qt remains optional development
source and is not installed by the base requirements.

Publication remains source only. Application GPL-3.0-or-later notices remain;
Python/Tcl/Tk permissive terms do not require changing the application's license.
Dependency obligations are recorded in THIRD_PARTY.md. Private subscription
configuration and runtime data are excluded. Search-service/copyright/privacy
rules remain applicable; this app does not grant rights to retrieved material.

The maintainer reports original or AI-generated code, with no known copied
project code. Reassess if attribution or conflicting provenance is discovered.

Historical distributions are documented in HISTORICAL_DISTRIBUTION.md. A
framework migration or deleting a repository does not erase past obligations.
Retain historical notices and source directions. Future public binary releases
require a file/version inventory, full third-party notices, matching GPL/MPL
source materials and actual frozen-build verification. No public binary release
is authorized by this review.
