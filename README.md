# TSearch-DS

Windows desktop search for magnet, eD2k and torrent links, built with Python / PyQt5.

Source-only distribution: this repository and its releases do not provide precompiled applications or bundled third-party binaries.

## Features

- Independent concurrent search tabs, streaming results and duplicate merging.
- Six columns: name, resource count, file size, file type, link and source. All origins of a merged resource are shown.
- Public index adapters including API Bay, Nyaa, DMHY, RuTor and Internet Archive. Searches do not require an index account.
- eD2k server/Kad adapters and background tracker enrichment.
- Optional local mihomo proxy; users supply their own subscription or import nodes. No default subscription is embedded.
- Multiselect copying, sorting, filtering and movable/resizable columns. No saved search-history database; see PRIVACY.md for network and diagnostic traces.

## Run and build

Install Python, then `python -m pip install -r requirements.txt`.

Run `python main.py`. On first launch, enter a subscription or cancel to use a direct connection. Use the proxy menu to change the subscription or import nodes.

Build with `powershell -File build/build-public.ps1`. Put an official `mihomo.exe` beside the generated EXE. For portable mode create an empty `portable.txt` and an empty `run` directory beside it. Obtain the proxy core separately from the official mihomo project: https://github.com/MetaCubeX/mihomo.

Subscriptions are saved locally as plaintext in `data/settings.json` in portable mode, or in the user's application data directory otherwise. Do not share a working directory that has been configured with personal credentials.

## Tests

`python tests/test_source_column.py`

`python tests/test_subscription_setup.py`

`python tests/test_public_sources.py`

`python tests/test_gate.py`

`python tests/test_timeouts.py`

`python tests/test_privacy.py`

Tests above do not query public indexes. `tests/test_tabs.py` also checks concurrent searches and requires internet access.

## Scope and licensing

Search coverage depends on enabled sources, their availability and network conditions. The app searches and copies links; it is not a downloader. Only access and redistribute material you are entitled to use.

GPL-3.0-or-later; see COPYING. PyQt5/Qt and mihomo have their own licensing requirements. If you build and redistribute binaries yourself, you must separately satisfy the licenses of all bundled components. Do not upload private subscription configuration, personal ZIPs or runtime data.

## Release and privacy policy

Public releases contain source only. The proxy core is not automatically downloaded or installed; obtain it yourself from the official mihomo project. See NOTICE.md, THIRD_PARTY.md, PRIVACY.md and CONTRIBUTING.md. External search results do not confer copyright permission, and service access must follow applicable law and site terms.

## Release review

See [COMPLIANCE_REVIEW.md](COMPLIANCE_REVIEW.md) for the completed controls and remaining limits, and [HISTORICAL_DISTRIBUTION.md](HISTORICAL_DISTRIBUTION.md) for withdrawn-binary license/source directions. Current publication remains source-only.
