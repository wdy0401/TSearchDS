# Release review — 2026-10-05

This is a documented project review, not a legal opinion or compliance certification.

## Current publication

Public releases contain application source only. No EXE, DLL, installed package,
subscription or usable node configuration is distributed in the current source
archive. The earlier public executable asset was withdrawn. Older source snapshots
remain available as historical records; use the latest main branch for corrections.

The maintainer confirmed that the original Python and Tauri code was self-developed
or AI-generated, with no known copied project code. This is a provenance statement,
not proof of originality or a patent clearance. Preserve any attribution that is
later discovered and reassess affected code before redistribution.

## Completed controls

- Application GPL-3.0-or-later notice, full license, per-file SPDX identifiers and
  attribution conventions retained. Upstream files keep their own notices.
- Dependency licensing index, contribution rules and network/privacy disclosure added.
- No default personal subscription; public fixtures use synthetic credentials.
- Public archive checked against known private subscriptions and original proxy values.
- Startup does not silently download/install the proxy core; users obtain it separately.
- HTTP diagnostics redact URL paths/credentials and do not print request exception text;
  subscription-failure messages do not print the provider URL.
- Regression checks cover manual core setup, URL redaction, subscription setup,
  source-column merging, public parsers, browser history protection and timeouts.
- Historical build license/recipe/patch evidence and dependency source directions
  are preserved in HISTORICAL_DISTRIBUTION.md and the associated materials archive.

## Limits and release conditions

Only source releases are cleared by this internal workflow. This does not declare
that any former or future binary bundle meets every applicable license. Do not
republish the withdrawn bundle until exact contents, dependency source access,
notices, rebuild/library replacement requirements and provider terms are verified.

For future personal builds, prefer a clean python.org Python virtual environment
and documented dependencies; review the exact licenses before any external release.
Conda package-provider terms are separate from individual upstream library licenses.

Publicly accessible indexes are not necessarily authorized for every automated use.
This review has not obtained permissions from search sites or verified every site's
terms. Use must respect their access restrictions, applicable network rules and
copyright. No login/CAPTCHA/paywall bypass is provided by this project. Results are
links, not a grant of rights to the underlying content. Site-specific restrictions
may require disabling an adapter. A disclaimer cannot cure prohibited activity.

No encrypted local storage or anonymity guarantee is claimed. Logs, crash reports,
clipboard, proxy providers, third-party services and OS traces must be considered
before sharing data or deploying the software for others. Review PRIVACY.md.

The GPL notice does not exclude mandatory rights or liabilities. A deployment,
commercial distribution or disputed copyright question may require legal review
in the relevant jurisdiction; that conclusion cannot be replaced by a code scan.
