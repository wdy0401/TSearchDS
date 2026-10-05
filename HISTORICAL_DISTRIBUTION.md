# Historical binary distribution materials

The public executable ZIP was withdrawn. Current releases contain source only.
The accompanying licenses/historical-build-materials.zip preserves upstream license notices, exact cached build recipes
and patches from the historical build environment. Upstream text is unmodified
and retains its own licensing; the application GPL notice does not relicense it.

Historical TSearchDS.exe SHA-256: `2ed41ed4bead8a6522647939e2dc17d4527229a739c34b1bf158982872199ff5`.

The original application source and build spec are retained in release
[v2026.10.05](https://github.com/wdy0401/TSearchDS/releases/tag/v2026.10.05).
For dependency source, extract `licenses/historical-build-materials.zip` and consult `licenses/historical/build-inputs.json` and each
package's `recipe/meta.yaml`: `source.url` gives the upstream download and
`source.sha256` its expected digest. Apply the accompanying recipe patches and
follow its build scripts. Some recipe source entries are build tools/prebuilt
inputs rather than source code; they are explicitly retained as build evidence.

Confirmed core versions: PyQt 5.15.11 (GPL-3.0-only), PyQt5-sip 12.17.0
(GPL-3.0-only), Qt 5.15.2 (package metadata: LGPL-3.0-only), Python 3.13.9.
The application can remain GPL-3.0-or-later; a bundle containing GPLv3-only
dependencies must respect their narrower terms.

Mihomo v1.19.32 source: https://github.com/MetaCubeX/mihomo/tree/v1.19.32
and https://github.com/MetaCubeX/mihomo/archive/refs/tags/v1.19.32.zip .
Mihomo license: https://github.com/MetaCubeX/mihomo/blob/v1.19.32/LICENSE .

The manifest is a conservative list of build-analysis inputs, not a claim that
every input was shipped or that all redistribution obligations are satisfied.
It may include unused modules from the build environment. Microsoft runtimes
and package-provider terms require separate treatment; a source URL cannot
replace a proprietary redistribution permission. If any required source link
becomes unavailable, it must be restored or replaced with equivalent access.

Do not resume binary distribution until the actual archive contents, source
availability, all notices and rebuild/library-replacement instructions have
been checked. Anaconda package/provider terms must also be reviewed for the
context of use. A future public build should use a clean, documented environment
and a complete dependency inventory, rather than an entire desktop environment.
