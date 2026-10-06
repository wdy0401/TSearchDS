# Dependency licensing — current Tkinter edition

The default interface is tkinter/ttk (Python standard library). The default
build excludes all PyQt/PySide/Qt modules. The optional Qt source backend is
for development and requires requirements-qt.txt; its LGPL/GPL obligations
apply if anyone distributes it. No dependency implementations are vendored
in the public source archive.

| Component | License / authoritative source |
| --- | --- |
| Python / tkinter | PSF and incorporated notices: https://docs.python.org/3/license.html |
| Tcl/Tk | Permissive Tcl/Tk terms: https://github.com/tcltk/tk/blob/core-8-6-branch/license.terms |
| Requests | Apache-2.0: https://github.com/psf/requests |
| urllib3 / charset-normalizer / PyYAML | MIT; preserve upstream notices |
| idna | BSD-3-Clause; preserve upstream notices |
| certifi | MPL-2.0: https://github.com/certifi/python-certifi ; provide source of distributed covered files, including modifications |
| lxml / libxml2 / libxslt | Preserve package and bundled-library notices: https://github.com/lxml/lxml/blob/master/LICENSES.txt |
| PyInstaller | GPL with bootloader exception and file-specific notices: https://github.com/pyinstaller/pyinstaller/blob/develop/COPYING.txt |
| mihomo (separate optional executable) | GPLv3: https://github.com/MetaCubeX/mihomo/blob/Meta/LICENSE |
| PySide6/Qt (optional, excluded from default binary) | Module-dependent LGPL/GPL/commercial: https://www.qt.io/development/open-source-lgpl-obligations |

## Distribution

Public publication remains application source only; no EXE, DLL, mihomo or
private proxy configuration is included. Application code retains its existing
GPL-3.0-or-later license. Switching frameworks does not revoke earlier grants.

For a future binary release inventory the actual files and exact versions,
preserve their complete license/copyright notices, supply this application's
corresponding source and build instructions under GPL, and satisfy MPL and
other covered-component source requirements. If mihomo is distributed, provide
its matching corresponding source and build materials; a generic upstream link
alone is not a substitute. System fonts are used by family name, never bundled.

Removing PyQt removes that dependency's obligations from new Tk-only builds;
it does not resolve historical binary distributions retroactively. Preserve
HISTORICAL_DISTRIBUTION.md and its evidence. Search-service terms, copyright
of retrieved content, privacy, and local proxy regulations remain separate
from GUI licensing. No zero-risk or legal-compliance certification is claimed.
