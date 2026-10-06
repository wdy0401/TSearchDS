# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""Automated gates for publishing the source tree.

These checks are deliberately mechanical: they catch the failure modes that
turned up in real releases (a real proxy node pasted into a fixture, a
dependency that is imported but not declared, a license header that went
missing).  They are a release gate, not a legal review -- see
COMPLIANCE_REVIEW.md for the parts that cannot be automated.
"""
import ast
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: Directories that never reach the public archive (runtime data, build
#: artefacts, historic binary releases).
SKIP_DIRS = {'.git', 'dist', 'release', 'data', 'run', '__pycache__',
             '.workbuddy', 'build'}
SCAN_SUFFIXES = ('.py', '.md', '.txt', '.json', '.yaml', '.yml', '.spec')

#: Hosts a test fixture is allowed to point at: RFC 2606 reserved names and
#: RFC 5737 documentation prefixes.
_FIXTURE_HOST_OK = re.compile(
    r"^(?:[\w.-]+\.)?(?:example|invalid|test)(?:\.[a-z]{2,})?$"
    r"|^localhost$|^127\.0\.0\.1$|^::1$"
    r"|^(?:192\.0\.2|198\.51\.100|203\.0\.113)\.\d{1,3}$", re.I)

#: Proxy node schemes.  A URI with one of these and a host is a node, not a
#: web link, so its host must be a reserved/documentation value.  The host has
#: to look routable (a dot or a bracketed IPv6 literal) so that comments like
#: ``ss://base64(...)@host:port`` are not mistaken for a real address.
_NODE_URI = re.compile(
    r"\b(vless|vmess|trojan|ss|ssr|hysteria2?|tuic)://[^\s\"'()]*?@"
    r"([A-Za-z0-9_-]+\.[A-Za-z0-9._-]+|\[[0-9A-Fa-f:]+\]|\d+\.\d+\.\d+\.\d+)")

#: Third-party top-level import name -> package that must appear in
#: requirements.txt.  Anything imported but missing here fails the test.
DECLARED_DEPENDENCIES = {
    'PySide6': 'PySide6-Essentials',
    'requests': 'requests',
    'urllib3': 'urllib3',
    'yaml': 'PyYAML',
    'lxml': 'lxml',
}

_USER_PATH = re.compile(r"[A-Za-z]:\\+(Users|tmp)\\", re.I)


def _scan_files():
    """Files that ship in the public source tree."""
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        rel = path.relative_to(ROOT)
        # only the packaging recipe itself is tracked under build/; everything
        # else there is a working copy or a historic export
        if rel.parts[0] in SKIP_DIRS and rel.parts[:2] != ('build',
                                                           'TSearchDS.spec'):
            continue
        if path.name == 'test_release_compliance.py':
            continue  # this file quotes the patterns it checks for
        yield path, rel


class ReleaseComplianceTests(unittest.TestCase):

    def test_no_real_proxy_node_in_fixtures(self):
        """A published fixture must never carry a usable node address."""
        bad = []
        for path, rel in _scan_files():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                for _scheme, host in _NODE_URI.findall(line):
                    if not _FIXTURE_HOST_OK.match(host):
                        bad.append("%s:%d host=%s" % (rel, lineno, host))
        self.assertEqual(bad, [],
                         "non-synthetic proxy host in published fixture; "
                         "use example.com or an RFC 5737 address")

    def test_every_third_party_import_is_declared(self):
        """requirements.txt must list every non-stdlib import of the app."""
        stdlib = set(getattr(sys, "stdlib_module_names", ()))
        used = set()
        for base in (ROOT / "src",):
            for path in base.rglob("*.py"):
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"))
                except (OSError, SyntaxError):
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            used.add(alias.name.split(".")[0])
                    elif isinstance(node, ast.ImportFrom) and not node.level:
                        if node.module:
                            used.add(node.module.split(".")[0])
        try:
            tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            self.fail("main.py does not parse")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    used.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and not node.level:
                if node.module:
                    used.add(node.module.split(".")[0])

        third_party = {m for m in used
                       if m not in stdlib and m not in ("src", "tests")}
        unknown = sorted(third_party - set(DECLARED_DEPENDENCIES))
        self.assertEqual(unknown, [],
                         "imported but not declared in requirements.txt / "
                         "THIRD_PARTY.md: %s" % unknown)

        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8") + (ROOT / "requirements-qt.txt").read_text(encoding="utf-8")
        for module, package in DECLARED_DEPENDENCIES.items():
            if module in third_party:
                self.assertIn(package, requirements,
                              "%s (%s) missing from requirements.txt"
                              % (module, package))

    def test_source_files_carry_the_license_header(self):
        missing = []
        for path in sorted((ROOT / "src").rglob("*.py")):
            try:
                head = path.read_text(encoding="utf-8")[:400]
            except OSError:
                continue
            if "SPDX-License-Identifier" not in head:
                missing.append(str(path.relative_to(ROOT)))
        if "SPDX-License-Identifier" not in (ROOT / "main.py").read_text(
                encoding="utf-8")[:400]:
            missing.append("main.py")
        self.assertEqual(missing, [], "missing SPDX header: %s" % missing)

    def test_pyqt5_is_fully_replaced(self):
        """Application imports must not load the removed PyQt5 backend."""
        leftovers = []
        for path, rel in _scan_files():
            if rel.suffix != '.py':
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                if re.search(r"\bPyQt5\b", line):
                    leftovers.append("%s:%d %s" % (rel, lineno, line.strip()[:70]))
        self.assertEqual(leftovers, [], "PyQt5 reference remains: %s" % leftovers)

    def test_no_machine_specific_absolute_paths(self):
        bad = []
        for path, rel in _scan_files():
            if rel.suffix != '.py':
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                if _USER_PATH.search(line):
                    bad.append("%s:%d %s" % (rel, lineno, line.strip()[:70]))
        self.assertEqual(bad, [], "hard-coded user path: %s" % bad)

    def test_runtime_data_is_gitignored(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for entry in ("data/", "settings.json", "user_nodes.json",
                      "nodes.yaml", "config.yaml", "mihomo.log"):
            self.assertIn(entry, ignore,
                          "%s must stay out of the public archive" % entry)


if __name__ == '__main__':
    unittest.main()
