"""Repository-side checks for sources/10_SIMCONNECT_FUNCTION_DICTIONARY.md.

The full inventory check in the expanded preflight report also reads the
installed SDK header and DLL export table, which only exist on the owner's
workstation. This test repeats the part that the repository alone can prove:
every row is well formed, the implemented rows match the adapter's SDK
declarations exactly, and the stated totals agree with the table. It reads the
source with ``ast`` and never imports the ctypes binding.
"""

import ast
import re
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DICTIONARY = ROOT / "sources" / "10_SIMCONNECT_FUNCTION_DICTIONARY.md"
STATUS_PAGE = ROOT / "sources" / "07_STATUS_AND_EVIDENCE.md"
STATUSES = ("Core path", "Offline path", "Experimental", "Host helper", "Deprecated", "Unsupported", "Header only")
IMPLEMENTED = {"Core path", "Offline path", "Experimental", "Host helper"}
# Same row pattern as the expanded preflight report's inventory script.
ROW = re.compile(
    r"\| (?:\[(SimConnect_\w+)\]\(([^)]+)\)|`(SimConnect_\w+)`) \| .+? "
    r"\| (" + "|".join(STATUSES) + r") "
    r"\| \[(\d+)\]\(<C:/MSFS SDK/SimConnect SDK/include/SimConnect\.h:(\d+)>\) \|")
NOT_IN_REPOSITORY = "not present in the repository"
# Per-status split recorded by the expanded SDK DLL preflight report.
PREFLIGHT_SPLIT = {"Core path": 13, "Offline path": 47, "Experimental": 3, "Host helper": 4,
                   "Deprecated": 22, "Unsupported": 1, "Header only": 1}


def dictionary_rows():
    rows = {}
    for line in DICTIONARY.read_text(encoding="utf-8").splitlines():
        match = ROW.fullmatch(line)
        if match:
            name = match.group(1) or match.group(3)
            if name in rows:
                raise AssertionError(f"{name} appears twice in the dictionary")
            rows[name] = (match.group(4), int(match.group(5)), int(match.group(6)))
    return rows


def adapter_signatures():
    tree = ast.parse((ROOT / "bbc_core" / "simconnect_abi.py").read_text(encoding="utf-8"))
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [getattr(node, "target", None)]
        if any(isinstance(target, ast.Name) and target.id == "SIGNATURES" for target in targets):
            return {key.value for key in node.value.keys}
    raise AssertionError("SIGNATURES was not found in bbc_core/simconnect_abi.py")


class FunctionDictionaryTests(unittest.TestCase):
    def test_every_function_row_is_well_formed_and_unique(self):
        rows = dictionary_rows()
        table_lines = [line for line in DICTIONARY.read_text(encoding="utf-8").splitlines()
                       if re.match(r"\| (\[|`)SimConnect_", line)]
        self.assertEqual(len(rows), len(table_lines), "a function row does not match the inventory format")
        self.assertEqual(len(rows), 91)
        for name, (_status, shown, linked) in rows.items():
            with self.subTest(name=name):
                self.assertEqual(shown, linked)

    def test_implemented_rows_match_the_adapter_declarations(self):
        rows = dictionary_rows()
        implemented = {name for name, (status, _, _) in rows.items() if status in IMPLEMENTED}
        signatures = adapter_signatures()
        self.assertEqual(sorted(implemented - signatures), [], "dictionary marks these implemented but the adapter has no declaration")
        self.assertEqual(sorted(signatures - implemented), [], "adapter declares these but the dictionary does not mark them implemented")

    def test_stated_function_count_matches_the_table(self):
        text = DICTIONARY.read_text(encoding="utf-8")
        stated = re.search(r"The current adapter uses \*\*(\d+)\*\* functions", text)
        self.assertIsNotNone(stated)
        implemented = sum(1 for status, _, _ in dictionary_rows().values() if status in IMPLEMENTED)
        self.assertEqual(int(stated.group(1)), implemented)

    def test_dispatch_statuses_follow_the_live_evidence(self):
        rows = dictionary_rows()
        self.assertEqual(rows["SimConnect_CallDispatch"][0], "Core path")
        self.assertEqual(rows["SimConnect_GetNextDispatch"][0], "Host helper")
        text = DICTIONARY.read_text(encoding="utf-8")
        self.assertNotIn("Dialog-mode notification group requests remain pending", text)
        self.assertIn("request_dialog_notifications()", text)

    def test_status_counts_are_recorded_for_the_preflight(self):
        counts = Counter(status for status, _, _ in dictionary_rows().values())
        self.assertEqual(sum(counts.values()), 91)
        self.assertLessEqual(set(counts), set(STATUSES))
        # The exact split the expanded preflight report asserts (finding A138), so a
        # row cannot move between statuses without this test and that report noticing.
        self.assertEqual(dict(counts), PREFLIGHT_SPLIT)

    def test_page_states_the_same_split_as_its_table(self):
        text = DICTIONARY.read_text(encoding="utf-8")
        match = re.search(r"seven-status table: ((?:[A-Z][a-z]+(?: [a-z]+)? \d+(?:, )?){7})\.", text)
        self.assertIsNotNone(match, "the page should state the per-status split")
        stated = {name: int(count) for name, count in re.findall(r"([A-Z][a-z]+(?: [a-z]+)?) (\d+)", match.group(1))}
        self.assertEqual(stated, dict(Counter(status for status, _, _ in dictionary_rows().values())))

    def test_callback_dispatch_row_states_the_memory_bounds(self):
        text = DICTIONARY.read_text(encoding="utf-8")
        row = next(line for line in text.splitlines() if line.startswith("| [SimConnect_CallDispatch]"))
        self.assertIn("64 MB", row)
        self.assertIn("128 MB ends that dispatch with a provider error", row)
        source = (ROOT / "bbc_core" / "simconnect_sdk.py").read_text(encoding="utf-8")
        self.assertIn("_MAX_CALLBACK_BYTES = 64 * 1024 * 1024", source)
        self.assertIn("2 * _MAX_CALLBACK_BYTES", source)


class EvidenceLinkTests(unittest.TestCase):
    """Relative links either resolve in the clone or say they are local-only."""

    def check_page(self, page):
        problems = []
        for number, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1):
            for match in re.finditer(r"\]\((<[^>]+>|[^)\s]+)\)", line):
                link = match.group(1).strip("<>")
                if re.match(r"[a-z]+:", link, re.IGNORECASE) or link.startswith("#"):
                    continue
                target = (page.parent / link.split("#", 1)[0]).resolve()
                if target.exists():
                    continue
                # The note may follow the link directly or close the same sentence.
                if NOT_IN_REPOSITORY not in line[match.end():match.end() + 400]:
                    problems.append(f"{page.name}:{number} {link}")
        self.assertEqual(problems, [], "dead link without a 'not present in the repository' note")

    def test_status_and_evidence_links(self):
        self.check_page(STATUS_PAGE)

    def test_function_dictionary_links(self):
        self.check_page(DICTIONARY)


if __name__ == "__main__":
    unittest.main()
