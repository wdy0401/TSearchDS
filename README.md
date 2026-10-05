# TSearch-DS

Windows desktop search for magnet, eD2k and torrent links, built with Python / PyQt5.

## Features

- Independent concurrent search tabs, streaming results and duplicate merging.
- Six columns: name, resource count, file size, file type, link and source. All origins of a merged resource are shown.
- Public index adapters including API Bay, Nyaa, DMHY, RuTor and Internet Archive. Searches do not require an index account.
- eD2k server/Kad adapters and background tracker enrichment.
- Optional local mihomo proxy; users supply their own subscription or import nodes. No default subscription is embedded.
- Multiselect copying, sorting, filtering and movable/resizable columns. No persistent search history.

## Run and build

Install Python, then `python -m pip install -r requirements.txt`.

Run `python main.py`. On first launch, enter a subscription or cancel to use a direct connection. Use the proxy menu to change the subscription or import nodes.

Build with `powershell -File build/build-public.ps1`. Put an official `mihomo.exe` beside the generated EXE. For portable mode create an empty `portable.txt` and an empty `run` directory beside it. The packaged public ZIP already includes these files and the mihomo core.

Subscriptions are saved locally as plaintext in `data/settings.json` in portable mode, or in the user's application data directory otherwise. Share only the original clean public package, not a folder that has been configured with personal credentials.

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

GPL-3.0-or-later; see COPYING. PyQt5/Qt and mihomo have their own licensing requirements. Binary public packages include application source, mihomo v1.19.32 source and license texts. Do not upload private subscription configuration, personal ZIPs or runtime data.
