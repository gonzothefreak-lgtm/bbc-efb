"""The module in docs/MODULE_AUTHORING.md must work as written (sources/08 acceptance item:
a developer using the docs builds a module the EFB discovers, which performs a supported
read and write with no Core edits or separate SimConnect setup)."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from uuid import uuid4

from bbc_core.contract import CommandState
from bbc_core.core import Core
from bbc_core.simulated import SimulatedProvider
from bbc_efb.modules import ModuleHost, ModuleStatus, core_api_provider

GUIDE = Path(__file__).resolve().parent.parent / "docs" / "MODULE_AUTHORING.md"


def guide_blocks():
    text = GUIDE.read_text(encoding="utf-8")
    manifest = json.loads(re.search(r"```json\n(.*?)```", text, re.S).group(1))
    code = re.search(r"```python\n(# com1_helper/main.py\n.*?)```", text, re.S).group(1)
    return manifest, code


class GuideExampleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name)
        self.path_before = list(sys.path)
        self.addCleanup(self._check_path)
        self.core = Core(SimulatedProvider())
        self.core.connect()
        self.addCleanup(self.core.disconnect)

    def _check_path(self):
        self.assertEqual(sys.path, self.path_before, "module folders must be removed from sys.path")

    def install(self, package):
        manifest, code = guide_blocks()
        manifest["entry"] = manifest["entry"].replace("com1_helper", package, 1)
        folder = self.data / "modules" / manifest["id"]
        (folder / package).mkdir(parents=True)
        (folder / package / "__init__.py").write_text("", encoding="utf-8")
        (folder / package / "main.py").write_text(code, encoding="utf-8")
        (folder / "module.json").write_text(json.dumps(manifest), encoding="utf-8")
        return manifest["id"]

    def test_guide_manifest_parses_clean(self):
        host = ModuleHost(self.data, core_api_provider(self.core))
        host.set_experimental_enabled(True)
        module_id = self.install(f"com1_helper_{uuid4().hex[:8]}")
        record = host.scan()[0]
        self.assertEqual(record.key, module_id)
        self.assertIs(record.status, ModuleStatus.DISABLED, record.problems)
        notes = [w for w in record.warnings if w.code.value == "note"]
        self.assertEqual(notes, [])

    def run_example(self, grant):
        host = ModuleHost(self.data, core_api_provider(self.core))
        host.set_experimental_enabled(True)
        module_id = self.install(f"com1_helper_{uuid4().hex[:8]}")
        host.scan()
        host.set_enabled(module_id, True)
        if grant:
            host.grant(module_id, ["radios"])
        record = host.load(module_id)
        self.addCleanup(host.close)
        self.assertIs(record.status, ModuleStatus.LOADED, record.problems)
        return host, host.instance(module_id)

    def test_example_reads_and_writes(self):
        host, module = self.run_example(grant=True)
        for _ in range(3):
            self.core.pump()
            host.tick()
        self.assertIsInstance(module.active, int)
        state = module.swap()
        self.assertIn(state, (CommandState.ACCEPTED, CommandState.SENT, CommandState.CONFIRMED))

    def test_example_without_grant_explains(self):
        host, module = self.run_example(grant=False)
        self.assertIn("Grant it in Settings", module.swap())
        # The host-side capability probe (all requested groups) was closed again.
        self.assertEqual(len(self.core._consumers), 1)

    def test_example_unknown_after_disconnect(self):
        host, module = self.run_example(grant=True)
        self.core.pump()
        host.tick()
        self.core.disconnect()
        host.tick()
        self.assertIsNone(module.active)
        self.core.connect()


if __name__ == "__main__":
    unittest.main()
