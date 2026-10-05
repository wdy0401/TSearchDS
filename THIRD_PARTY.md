# Third-party dependencies

This source repository imports dependencies; it does not vendor their implementations.
The table is a guide to upstream licensing, not a binary distribution license inventory.
Check the exact versions you install and preserve their notices when redistributing them.

| Component | Upstream license / source |
| --- | --- |
| Python | PSF license and bundled notices: https://docs.python.org/3/license.html |
| PyQt5 | GPLv3 or commercial; not LGPL: https://www.riverbankcomputing.com/software/pyqt/ |
| Qt | Module/version-dependent GPL, LGPL or commercial: https://www.qt.io/development/open-source-lgpl-obligations |
| Requests | Apache-2.0: https://github.com/psf/requests |
| PyYAML | MIT: https://github.com/yaml/pyyaml |
| lxml | BSD, with additional notices for libxml2/libxslt: https://github.com/lxml/lxml/blob/master/LICENSES.txt |
| PyInstaller (build tool) | GPL-2.0-or-later with bootloader exception, plus file-specific licenses: https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt |
| mihomo (optional separate executable) | GPLv3: https://github.com/MetaCubeX/mihomo/blob/Meta/LICENSE |

Requests' transitive dependencies and any DLLs collected by a local build need
their own review. Neither this table nor the application's GPL notice relicenses
third-party software. GPL-compatible application source does not by itself satisfy
all obligations for a compiled bundle.

Search sites, trackers, eD2k servers and Kad peers are external services, not
project dependencies or affiliates. Their availability and terms are independent.
