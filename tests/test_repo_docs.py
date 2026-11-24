"""Checks that README.md, docs/DEVELOPMENT.md and sources/07-08 say only what the code supports."""

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")
DEVELOPMENT = (ROOT / "docs" / "DEVELOPMENT.md").read_text(encoding="utf-8")
STATUS = (ROOT / "sources" / "07_STATUS_AND_EVIDENCE.md").read_text(encoding="utf-8")
DECISIONS = (ROOT / "sources" / "08_DECISIONS_AND_READINESS.md").read_text(encoding="utf-8")
FULL_SUITE = 'python -m unittest discover -s tests -t . -p "test_*.py"'


def sdk_default(keyword):
    tree = ast.parse((ROOT / "bbc_core" / "simconnect_sdk.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "SimConnectSdk":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    args = item.args
                    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
                        if arg.arg == keyword:
                            return ast.literal_eval(default)
    raise AssertionError(f"SimConnectSdk.__init__ has no keyword {keyword}")


class ReadmeTests(unittest.TestCase):
    def test_entry_point_is_described_as_present(self):
        self.assertNotIn("once that entry point is present", README)
        self.assertIn("bbc_efb.__main__:main", README)
        self.assertTrue((ROOT / "bbc_efb" / "__main__.py").is_file())

    def test_ci_is_described_as_configured_not_as_run(self):
        self.assertNotIn("GitHub Actions runs this guarded suite", README)
        self.assertIn("has not run on GitHub yet", README)
        self.assertIn("Python 3.10, 3.11 and 3.12", README)

    def test_publish_claims_match_the_script(self):
        self.assertNotIn("refuses large or compressed binaries it cannot inspect", README)
        self.assertIn("opaque binaries over 8 MB", README)
        self.assertIn("byte-scanned only", README)
        script = (ROOT / "publish.ps1").read_text(encoding="ascii")
        self.assertIn("$maxOpaqueBinaryBytes = 8MB", script)

    def test_dispatch_default_matches_the_adapter(self):
        callback = sdk_default("callback_dispatch")
        self.assertIs(callback, True)
        self.assertIn("callback dispatch (`SimConnect_CallDispatch`) by default", README)

    def test_logging_relocation_and_modules_page_match_the_code(self):
        logging_setup = (ROOT / "bbc_efb" / "logging_setup.py").read_text(encoding="utf-8")
        settings = (ROOT / "bbc_efb" / "settings.py").read_text(encoding="utf-8")
        self.assertIn("then moves to `<data folder>/logs`", README)
        for name in ("def relocate_logging", "def release_folder"):
            self.assertIn(name, logging_setup)
        self.assertIn("**Modules (Experimental)**", README)
        self.assertIn('"Modules (Experimental)"', settings)
        self.assertIn("It is off by default", README)

    def test_listed_packages_exist(self):
        for package in ("bbc_core", "bbc_core.ipc", "bbc_efb", "bbc_efb.logbook_views", "bbc_efb.modules",
                        "bbc_recorder"):
            self.assertIn(f"`{package}`", README)
            self.assertTrue((ROOT.joinpath(*package.split(".")) / "__init__.py").is_file(), package)

    def test_test_commands_use_the_repository_root_as_top_level(self):
        for name, text in (("README.md", README), ("docs/DEVELOPMENT.md", DEVELOPMENT),
                           ("pyproject.toml", (ROOT / "pyproject.toml").read_text(encoding="utf-8"))):
            with self.subTest(document=name):
                self.assertIn("discover -s tests -t . -p", text)
                self.assertNotRegex(text, r'discover -s tests -p "test_\*\.py"')


class StatusPageTests(unittest.TestCase):
    def test_probe_script_is_not_said_to_be_gitignored(self):
        self.assertNotIn("the live probe script and the recovered R3.1 candidate are excluded", STATUS)
        self.assertIn("The live probe script is not present in the repository.", STATUS)

    def test_ci_and_guard_differences_are_stated_honestly(self):
        self.assertNotIn("and CI runs it", STATUS)
        self.assertIn("CI workflow is configured to run it", STATUS)
        self.assertIn("ctypes.dlopen(None)", STATUS)

    def test_round_three_rows_are_offline_only(self):
        rows = [line for line in STATUS.splitlines() if line.startswith("| Audit fixes, round 3")
                or line.startswith("| Package discovery and version")]
        self.assertEqual(len(rows), 2)
        for row in rows:
            with self.subTest(row=row[:40]):
                self.assertIn("offline only", row.casefold().replace("(", "").replace(")", ""))
        self.assertIn("No live MSFS session", rows[0])
        self.assertIn("no GitHub CI run", rows[0])


class DecisionPageTests(unittest.TestCase):
    def section(self):
        start = DECISIONS.index("### Audit fixes, round 3")
        return DECISIONS[start:DECISIONS.index("\n### ", start + 1)]

    def test_round_three_items_are_proposals_and_none_is_ticked(self):
        items = re.findall(r"^- \[(.)\] (.+)$", self.section(), re.MULTILINE)
        self.assertGreaterEqual(len(items), 6)
        for mark, text in items:
            with self.subTest(item=text[:40]):
                self.assertEqual(mark, " ", "owner-approval items must stay unticked")
                self.assertRegex(text, r"^(Proposal|Owner decision):")

    def test_existing_owner_approval_items_stay_unticked(self):
        for item in ("Package and verify the first Windows build", "Confirm automatic Recorder rules with the owner",
                     "With separate approval, validate the release candidate"):
            line = next(line for line in DECISIONS.splitlines() if item in line)
            self.assertTrue(line.startswith("- [ ] "), line[:60])


if __name__ == "__main__":
    unittest.main()
