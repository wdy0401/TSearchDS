# TSearch-DS

Windows desktop search for magnet, eD2k and torrent links, built with Python / Tkinter (ttk).

Source-only distribution: this repository and its releases do not provide precompiled applications or bundled third-party binaries.

## Features

- Independent concurrent search tabs, streaming results and duplicate merging.
- Six columns: name, resource count, file size, file type, link and source. All origins of a merged resource are shown.
- Public index adapters including API Bay, Nyaa, DMHY, RuTor and Internet Archive. Searches do not require an index account.
- eD2k server/Kad adapters and background tracker enrichment.
- Optional local mihomo proxy; users supply their own subscription or import nodes. No default subscription is embedded.
- Multiselect copying, sorting, filtering and movable/resizable columns. No saved search-history database; see PRIVACY.md for network and diagnostic traces.
- English and Simplified Chinese interface. English is the default; switch at runtime from the "Language" menu without losing open tabs, results or selections.

## User interface

The default UI uses Python's standard tkinter/ttk and permissively licensed
Tcl/Tk. The default EXE excludes all Qt bindings and libraries. An optional
PySide6 development backend is available with `requirements-qt.txt` and
`python main.py --ui qt`; see THIRD_PARTY.md for its separate obligations.

## Run and build

Install Python, then `python -m pip install -r requirements.txt`.

Run `python main.py`. On first launch, enter a subscription or cancel to use a direct connection. Use the proxy menu to change the subscription or import nodes.

Build with `powershell -File build/build-public.ps1`. Put an official `mihomo.exe` beside the generated EXE. For portable mode create an empty `portable.txt` and an empty `run` directory beside it. Obtain the proxy core separately from the official mihomo project: https://github.com/MetaCubeX/mihomo.

Subscriptions are saved locally as plaintext in `data/settings.json` in portable mode, or in the user's application data directory otherwise. Do not share a working directory that has been configured with personal credentials.

## Tests

`python tests/test_public_sources.py`

`python tests/test_tkinter_ui.py`

`python tests/test_release_compliance.py` (source-release gate: declared dependencies, no PyQt5 leftovers, synthetic proxy fixtures only, SPDX headers)

`python tests/test_gate.py`

`python tests/test_timeouts.py`

`python tests/test_privacy.py`

Tkinter tests run offline. Legacy Qt tests (test_language, test_source_column, test_subscription_setup) require requirements-qt.txt. Tests above do not query public indexes. `tests/test_tabs.py` also checks concurrent searches and requires internet access.

## Scope and licensing

Search coverage depends on enabled sources, their availability and network conditions. The app searches and copies links; it is not a downloader. Only access and redistribute material you are entitled to use.

GPL-3.0-or-later; see COPYING. Preserve third-party license notices and satisfy
source requirements when redistributing binaries. See THIRD_PARTY.md. Do not
upload private subscriptions, personal packages or runtime data.

## Release and privacy policy

Public releases contain source only. The proxy core is not automatically downloaded or installed; obtain it yourself from the official mihomo project. See NOTICE.md, THIRD_PARTY.md, PRIVACY.md and CONTRIBUTING.md. External search results do not confer copyright permission, and service access must follow applicable law and site terms.

## Release review

See [COMPLIANCE_REVIEW.md](COMPLIANCE_REVIEW.md) for the completed controls and remaining limits, and [HISTORICAL_DISTRIBUTION.md](HISTORICAL_DISTRIBUTION.md) for withdrawn-binary license/source directions. Current publication remains source-only.
