# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""Mechanical gates for the current Tk edition; not a legal certification."""
import ast
from pathlib import Path
import unittest
ROOT = Path(__file__).resolve().parents[1]

class TkLicensePolicyTests(unittest.TestCase):
    def test_base_install_has_no_qt(self):
        lines=(ROOT/'requirements.txt').read_text().splitlines()
        packages=[line.split('#')[0].strip() for line in lines]
        self.assertFalse(any(p.startswith(('PyQt', 'PySide')) for p in packages))

    def test_tk_modules_do_not_import_qt(self):
        for path in (ROOT/'src/ui_tk').glob('*.py'):
            tree=ast.parse(path.read_text(encoding='utf8'))
            for node in ast.walk(tree):
                names=[]
                if isinstance(node,ast.Import): names=[x.name for x in node.names]
                if isinstance(node,ast.ImportFrom): names=[node.module or '']
                self.assertFalse(any(n.startswith(('PyQt','PySide')) for n in names),path)

    def test_default_bundle_excludes_qt(self):
        tree=ast.parse((ROOT/'build/TSearchDS.spec').read_text(encoding='utf8'))
        values={n.targets[0].id: n.value for n in tree.body
                if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name)}
        excluded=ast.literal_eval(values['EXCLUDES'])
        for name in ('PyQt5','PyQt6','PySide2','PySide6','shiboken6'):
            self.assertIn(name,excluded)
        self.assertNotIn('tkinter',excluded)
        self.assertTrue((ROOT/'licenses/TK-LICENSE.txt').is_file())

if __name__ == '__main__': unittest.main()
