"""Static checks for packaging/build_portable.ps1, the pins and the packaging documents.

PowerShell isn't available here, so these tests check that the script refers
only to files that exist, calls only package_tools.py commands and options that
exist, stays ASCII for Windows PowerShell 5.1, and keeps its safety steps in the
right order. They also check the delivery documents for the facts they promise.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
SCRIPT = PACKAGING / "build_portable.ps1"
DEVELOPMENT = ROOT / "docs" / "DEVELOPMENT.md"


def load_tools():
    name = "bbc_package_tools_script_tests"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, PACKAGING / "package_tools.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def script_text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def strip_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def tool_parser_choices():
    """Return {command: set(option strings)} from package_tools' argparse setup."""

    import argparse
    tools = load_tools()
    captured = {}
    original = argparse.ArgumentParser.parse_args

    def capture(self, args=None, namespace=None):
        for action in self._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    captured[name] = {opt for a in sub._actions for opt in a.option_strings}
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = capture
    try:
        try:
            tools.main(["version"])
        except SystemExit:
            pass
    finally:
        argparse.ArgumentParser.parse_args = original
    return captured


class ScriptStructureTests(unittest.TestCase):
    def test_script_is_ascii_for_windows_powershell_5_1(self):
        raw = SCRIPT.read_bytes()
        self.assertTrue(raw.isascii(), "non-ASCII characters break Windows PowerShell 5.1 without a BOM")
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))

    def test_brackets_balance_outside_strings_and_comments(self):
        code = strip_comments(script_text())
        code = re.sub(r"'[^'\n]*'", "''", code)
        code = re.sub(r'"(?:`.|[^"`\n])*"', '""', code)
        for opening, closing in ("{}", "()", "[]"):
            with self.subTest(pair=opening + closing):
                self.assertEqual(code.count(opening), code.count(closing))

    def test_avoids_syntax_missing_from_windows_powershell_5_1(self):
        code = strip_comments(script_text())
        self.assertNotRegex(code, r"\?\?", "null-coalescing needs PowerShell 7")
        self.assertNotRegex(code, r"\$IsWindows", "$IsWindows needs PowerShell 6+")
        self.assertNotRegex(code, r"Join-Path\s+\S+\s+\S+\s+\S+", "Join-Path with 3 parts needs PowerShell 6+")
        self.assertNotIn("Compress-Archive", code, "Compress-Archive in 5.1 writes backslash entry names")
        self.assertNotRegex(code, r"&&|\|\|", "pipeline chain operators need PowerShell 7")

    def test_strict_error_handling(self):
        text = script_text()
        self.assertIn("$ErrorActionPreference = 'Stop'", text)
        self.assertIn("Set-StrictMode", text)
        self.assertIn("$PSNativeCommandUseErrorActionPreference = $false", text)
        self.assertIn("exit 1", text)

    def test_files_referenced_by_the_script_exist(self):
        text = script_text()
        for folder, parts in re.findall(r"Combine\(\$(packagingDir|root), ((?:'[^']+'(?:, )?)+)\)", text):
            names = re.findall(r"'([^']+)'", parts)
            base = PACKAGING if folder == "packagingDir" else ROOT
            path = base.joinpath(*names)
            if names[0] in ("build", "dist", "CHANGELOG.md"):
                continue  # created by the build or written by the owner
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertTrue(path.is_file(), f"{path} is referenced by build_portable.ps1 but missing")

    def test_package_tools_commands_and_options_exist(self):
        choices = tool_parser_choices()
        text = script_text()
        used = set(re.findall(r"\$tools\s+([a-z][a-z-]+)", text))
        used |= set(re.findall(r"@\('([a-z][a-z-]+)'", text))
        used |= set(re.findall(r"\$tools, '([a-z][a-z0-9-]+)'", text))
        used = {u for u in used if u in choices or "-" in u}
        self.assertTrue(used >= {"check-interpreter", "version", "check-entry-point", "check-version-info",
                                 "check-changelog", "run-tests", "test-status", "assemble-portable",
                                 "verify-portable", "source-zip", "verify-source", "sha256sums"}, used)
        for command in used:
            self.assertIn(command, choices, f"package_tools.py has no '{command}' command")
        all_options = set().union(*choices.values())
        for option in set(re.findall(r"(?<![\w-])(--[a-z][a-z-]+)", strip_comments(text))):
            if option in ("--noconfirm", "--clean", "--log-level", "--distpath", "--workpath", "--disable-pip-version-check",
                          "--no-input", "--no-deps", "--only-binary", "--requirement", "--no-index", "--find-links",
                          "--porcelain", "--untracked-files", "--simconnect-dll"):
                continue  # pip, PyInstaller and git options
            with self.subTest(option=option):
                self.assertIn(option, all_options)

    def test_safety_steps_happen_in_order(self):
        text = strip_comments(script_text())

        def position(marker):
            index = text.find(marker)
            self.assertNotEqual(index, -1, marker)
            return index

        order = ["check-interpreter", "'--no-deps'", "'pip', 'check'", "check-entry-point", "check-changelog",
                 "run-tests", "Stop-Build \"The offline tests did not pass", "'-m', 'PyInstaller'",
                 "assemble-portable", "verify-portable", "source-zip", "verify-source", "sha256sums"]
        positions = [position(marker) for marker in order]
        self.assertEqual(positions, sorted(positions), dict(zip(order, positions)))

    def test_pip_installs_only_pinned_wheels(self):
        text = script_text()
        for flag in ("'--no-deps'", "'--only-binary=:all:'", "'--requirement', $requirements"):
            self.assertIn(flag, text)

    def test_never_copies_simconnect_dll(self):
        code = strip_comments(script_text()).casefold()
        self.assertNotIn("simconnect.dll", code.replace("simconnect.dll.", ""))

    def test_failed_verification_removes_the_delivery(self):
        text = script_text()
        catch = text[text.index("verify-portable"):]
        self.assertRegex(catch, r"catch \{\s+# Never leave a ZIP behind that failed verification\.\s+Remove-BuildFolder \$deliveryDir")

    def test_removal_is_confined_to_build_and_dist(self):
        text = script_text()
        body = text[text.index("function Remove-BuildFolder"):text.index("try {")]
        self.assertIn("'dist'", body)
        self.assertIn("$buildDir", body)
        self.assertIn("Refusing to remove", body)
        for call in re.findall(r"Remove-BuildFolder (\$\w+)", text):
            self.assertIn(call, ("$venvDir", "$pyiWorkDir", "$pyiDistDir", "$reportDir", "$deliveryDir", "$Path"))

    def test_parameters_are_documented(self):
        header = script_text().split("# Builds the BBC EFB portable", 1)[0]
        params = re.findall(r"\]\s*\[(?:string|switch|int)\]\$(\w+)|^\s*\[(?:string|switch|int)\]\$(\w+)", header, re.M)
        names = {a or b for a, b in params}
        readme = (PACKAGING / "README.md").read_text(encoding="utf-8")
        self.assertTrue(names >= {"PythonVersion", "PythonExe", "Changelog", "WheelDir", "ReuseVenv", "AllowDirty",
                                  "ExcludeSimulated", "LaunchCheck", "LaunchCheckSeconds"}, names)
        for name in names:
            self.assertIn(f"-{name}", readme)


class PinTests(unittest.TestCase):
    def requirements(self):
        lines = (PACKAGING / "requirements-build.txt").read_text(encoding="utf-8").splitlines()
        return [line.split("#")[0].strip() for line in lines if line.split("#")[0].strip()]

    def test_every_requirement_is_pinned_exactly(self):
        for line in self.requirements():
            with self.subTest(line=line):
                self.assertRegex(line, r"^[A-Za-z0-9_.-]+==[0-9][0-9A-Za-z.]*(\s+--hash=sha256:[0-9a-f]{64})*$")

    def test_pyside6_pin_matches_the_ui_extra_and_its_parts(self):
        pins = dict(line.split("==", 1) for line in self.requirements())
        pins = {k.casefold().replace("_", "-"): v.split()[0] for k, v in pins.items()}
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'"PySide6>=([0-9.]+),<([0-9.]+)"', pyproject)
        self.assertIsNotNone(match, "pyproject.toml ui extra should keep a PySide6 range")
        low, high = (tuple(int(p) for p in v.split(".")) for v in match.groups())
        version = tuple(int(p) for p in pins["pyside6"].split("."))
        self.assertTrue(low <= version < high, (low, version, high))
        for part in ("pyside6-essentials", "pyside6-addons", "shiboken6"):
            self.assertEqual(pins[part], pins["pyside6"], part)
        self.assertIn("pyinstaller", pins)
        self.assertIn("pyinstaller-hooks-contrib", pins)

    def test_packaging_folder_is_not_a_python_package(self):
        self.assertFalse((PACKAGING / "__init__.py").exists(),
                         "packaging/__init__.py would shadow the PyPI 'packaging' module")


class DocumentTests(unittest.TestCase):
    def read(self, name):
        return (PACKAGING / name).read_text(encoding="utf-8")

    def test_limitations_say_live_msfs_is_unverified(self):
        text = self.read("LIMITATIONS.md")
        self.assertIn("Not verified against a live simulator", text)
        self.assertIn("**unverified**", text)
        for topic in ("SimConnect.dll", "code-signed", "Recorder", "frame times", "LGPL"):
            self.assertIn(topic, text)

    def test_readmes_explain_finding_the_users_simconnect_dll(self):
        for name in ("README.md", "PORTABLE_README.md"):
            text = self.read(name)
            with self.subTest(name=name):
                self.assertIn("MSFS_SDK", text)
                self.assertIn("--simconnect-dll", text)
                self.assertIn("SimConnect SDK", text)
                self.assertRegex(text, r"(?i)isn't (included|bundled)|never bundled")

    def test_dll_search_order_in_docs_matches_core(self):
        source = (ROOT / "bbc_core" / "simconnect_sdk.py").read_text(encoding="utf-8")
        self.assertIn('SDK_ENVIRONMENT_VARIABLE = "MSFS_SDK"', source)
        self.assertIn('Path(sdk) / "SimConnect SDK" / "lib" / "SimConnect.dll"', source)
        self.assertIn("_MEIPASS", source)

    def test_template_has_placeholders_and_current_version(self):
        text = self.read("CHANGELOG_TEMPLATE.md")
        tools = load_tools()
        self.assertIn(tools.CHANGELOG_TEMPLATE_MARKER, text)
        self.assertIn(tools.read_app_version(ROOT), text)

    def test_portable_readme_lists_every_shipped_document(self):
        text = self.read("PORTABLE_README.md")
        for name in load_tools().PORTABLE_DOCS:
            if name != "README.md":
                self.assertIn(f"`{name}`", text)

    def test_owner_proposals_are_recorded(self):
        text = self.read("README.md")
        self.assertIn("## Owner proposals", text)
        for topic in ("Freezer", "Dependency pins", "Two ZIPs", "Simulated provider", "Licence texts"):
            self.assertIn(topic, text)

    def test_relative_links_resolve(self):
        for path in [*PACKAGING.glob("*.md"), DEVELOPMENT]:
            text = path.read_text(encoding="utf-8")
            for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text):
                if "://" in target:
                    continue
                with self.subTest(document=path.name, target=target):
                    self.assertTrue((path.parent / target).exists(), f"{path.name} links to missing {target}")

    def test_development_guide_covers_both_platforms_and_layout(self):
        text = DEVELOPMENT.read_text(encoding="utf-8")
        for heading in ("## Set up on Windows", "## Set up on macOS", "## Run the tests", "## Project layout"):
            self.assertIn(heading, text)
        for item in ("QT_QPA_PLATFORM=offscreen", "tests/test_repo_offline_guard.py", "tests_wip", "bbc_recorder/",
                     "bbc_core/host.py", "packaging/", "core_bridge.py"):
            self.assertIn(item, text)

    def test_documents_are_free_of_personal_paths(self):
        for path in [*PACKAGING.iterdir(), DEVELOPMENT]:
            if path.is_file() and path.suffix in (".md", ".txt", ".ps1", ".py", ".spec"):
                text = path.read_text(encoding="utf-8")
                with self.subTest(path=path.name):
                    self.assertNotRegex(text, r"/Users/[a-z]|C:\\\\Users\\\\[A-Za-z]+\\\\(?!<)")


if __name__ == "__main__":
    unittest.main()
