"""Manifest parsing, version ranges and API compatibility for drop-in modules."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from bbc_efb.modules.manifest import (
    MANIFEST_MAX_BYTES, EntryPoint, ModuleManifest, ProblemCode, Version, VersionRange, api_compatibility,
    manifest_from_dict, read_manifest,
)


def good(**changes):
    payload = {
        "manifest_format": 1, "id": "example.radio_helper", "name": "Radio helper", "version": "1.2.0",
        "api_version": ">=0.1,<0.2", "entry": "radio_helper.main:create", "control_groups": ["radios"],
        "description": "Tunes COM1.", "author": "A. Pilot",
        "requires": {"capabilities": ["radio.com1.active_frequency"], "modules": [], "python_packages": []},
    }
    payload.update(changes)
    return payload


class VersionTests(unittest.TestCase):
    def test_parse_and_order(self):
        self.assertEqual(Version.parse("1").numbers, (1, 0, 0))
        self.assertEqual(Version.parse("0.1-draft").label, "draft")
        self.assertEqual(Version.parse("0.1-draft"), Version.parse("0.1"))
        self.assertLess(Version.parse("1.2.3"), Version.parse("1.10"))
        self.assertEqual(str(Version.parse("2.0.1+build5")), "2.0.1-build5")
        for bad in ("", "v1", "1.2.3.4", "1..2", None, 3):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                Version.parse(bad)

    def test_ranges(self):
        r = VersionRange.parse(">=0.1, <0.2")
        self.assertTrue(r.contains(Version.parse("0.1-draft")))
        self.assertTrue(r.contains(Version.parse("0.1.9")))
        self.assertFalse(r.contains(Version.parse("0.2")))
        self.assertFalse(r.contains(Version.parse("0.0.9")))

    def test_bare_version_is_compatible_range(self):
        zero = VersionRange.parse("0.1")
        self.assertTrue(zero.contains(Version.parse("0.1.4")))
        self.assertFalse(zero.contains(Version.parse("0.2")))
        one = VersionRange.parse("1.2")
        self.assertTrue(one.contains(Version.parse("1.9")))
        self.assertFalse(one.contains(Version.parse("1.1")))
        self.assertFalse(one.contains(Version.parse("2.0")))

    def test_operators(self):
        self.assertTrue(VersionRange.parse("==0.1").contains(Version.parse("0.1.0")))
        self.assertFalse(VersionRange.parse("!=0.1").contains(Version.parse("0.1")))
        self.assertTrue(VersionRange.parse(">0.1,<=1.0").contains(Version.parse("1.0")))
        for bad in ("", ">=", ">=0.1,", "=>0.1", "~0.1"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                VersionRange.parse(bad)


class EntryTests(unittest.TestCase):
    def test_entry(self):
        entry = EntryPoint.parse("pkg.sub:make")
        self.assertEqual((entry.module, entry.attribute, entry.top_level), ("pkg.sub", "make", "pkg"))
        self.assertEqual(EntryPoint.parse("pkg:Factory.create").attribute, "Factory.create")
        for bad in ("pkg", "pkg:", ":x", "a:b:c", "../evil:x", "pkg.class:x", "pkg-name:x", "pkg/x:y"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                EntryPoint.parse(bad)


class ManifestTests(unittest.TestCase):
    def test_good_manifest(self):
        result = manifest_from_dict(good())
        self.assertTrue(result.ok, result.problems)
        m = result.manifest
        self.assertEqual(m.id, "example.radio_helper")
        self.assertEqual(m.control_groups, ("radios",))
        self.assertEqual(m.requires.capabilities, ("radio.com1.active_frequency",))
        self.assertEqual(result.warnings, ())

    def test_minimal_manifest_defaults(self):
        payload = {"id": "mini", "name": "Mini", "version": "0.1", "api_version": "0.1", "entry": "mini:create"}
        result = manifest_from_dict(payload)
        self.assertTrue(result.ok, result.problems)
        self.assertEqual(result.manifest.control_groups, ())
        self.assertEqual(result.manifest.requires.modules, ())

    def test_missing_fields_all_reported(self):
        result = manifest_from_dict({"manifest_format": 1})
        self.assertFalse(result.ok)
        text = " ".join(p.message for p in result.problems)
        for key in ("id", "name", "version", "api_version", "entry"):
            self.assertIn(f'"{key}" is missing', text)
        self.assertTrue(all(p.code is ProblemCode.BAD_MANIFEST for p in result.problems))

    def test_invalid_values(self):
        cases = {
            "id": "Bad Id", "version": "one", "api_version": "latest", "entry": "nocolon",
            "control_groups": "radios", "requires": [], "name": 5, "manifest_format": "1",
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                result = manifest_from_dict(good(**{key: value}))
                self.assertFalse(result.ok)
                self.assertTrue(any(key.split(".")[0] in p.message or "requires" in p.message
                                    for p in result.problems), result.problems)

    def test_newer_manifest_format_is_explained(self):
        result = manifest_from_dict(good(manifest_format=2))
        self.assertFalse(result.ok)
        self.assertIn("format 2", result.problems[0].message)

    def test_bad_group_and_requirements(self):
        self.assertFalse(manifest_from_dict(good(control_groups=["Radios!"])).ok)
        self.assertFalse(manifest_from_dict(good(requires={"capabilities": ["nodot"]})).ok)
        self.assertFalse(manifest_from_dict(good(requires={"modules": ["example.radio_helper"]})).ok)
        self.assertFalse(manifest_from_dict(good(requires={"python_packages": ["not-a-name"]})).ok)
        self.assertFalse(manifest_from_dict(good(control_groups=["radios"] * 65)).ok)

    def test_duplicate_groups_collapsed(self):
        result = manifest_from_dict(good(control_groups=["radios", "radios", "electrical"]))
        self.assertEqual(result.manifest.control_groups, ("radios", "electrical"))

    def test_unknown_keys_are_warnings(self):
        result = manifest_from_dict(good(icon="x.png", requires={"aircraft": ["A320"]}))
        self.assertTrue(result.ok)
        self.assertEqual(len(result.warnings), 2)
        self.assertTrue(all(w.code is ProblemCode.NOTE for w in result.warnings))

    def test_text_length_limits(self):
        self.assertFalse(manifest_from_dict(good(name="x" * 81)).ok)
        self.assertFalse(manifest_from_dict(good(description="x" * 2001)).ok)

    def test_not_an_object(self):
        self.assertFalse(manifest_from_dict([1, 2]).ok)

    def test_build_for_builtins(self):
        manifest = ModuleManifest.build(id="builtin.radio", name="Radio", version="1.0", api_version="0.1",
                                        entry="builtin:radio", control_groups=("radios",))
        self.assertEqual(manifest.control_groups, ("radios",))
        with self.assertRaises(ValueError):
            ModuleManifest.build(id="Bad", name="", version="x", api_version="0.1", entry="a:b")


class ReadManifestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file(self):
        result = read_manifest(self.folder)
        self.assertIn("no module.json", result.problems[0].message)

    def test_invalid_json(self):
        (self.folder / "module.json").write_text("{not json", encoding="utf-8")
        self.assertIn("not valid JSON", read_manifest(self.folder).problems[0].message)

    def test_not_utf8(self):
        (self.folder / "module.json").write_bytes(b"\xff\xfe\x00{")
        self.assertIn("UTF-8", read_manifest(self.folder).problems[0].message)

    def test_too_large(self):
        (self.folder / "module.json").write_text(" " * (MANIFEST_MAX_BYTES + 1), encoding="utf-8")
        self.assertIn("larger than", read_manifest(self.folder).problems[0].message)

    def test_bom_accepted(self):
        (self.folder / "module.json").write_text("﻿" + json.dumps(good()), encoding="utf-8")
        self.assertTrue(read_manifest(self.folder).ok)


class CompatibilityTests(unittest.TestCase):
    def test_draft_api_is_compatible_with_warning(self):
        manifest = manifest_from_dict(good()).manifest
        blocking, warning = api_compatibility(manifest, "0.1-draft")
        self.assertIsNone(blocking)
        self.assertIs(warning.code, ProblemCode.DRAFT_API)

    def test_mismatch_names_both_versions(self):
        manifest = manifest_from_dict(good(api_version=">=1.0")).manifest
        blocking, _ = api_compatibility(manifest, "0.1-draft")
        self.assertIs(blocking.code, ProblemCode.API_VERSION_MISMATCH)
        self.assertIn(">=1.0", blocking.message)
        self.assertIn("0.1-draft", blocking.message)

    def test_stable_api_no_warning(self):
        manifest = manifest_from_dict(good(api_version="1.0")).manifest
        self.assertEqual(api_compatibility(manifest, "1.3"), (None, None))

    def test_unreadable_host_version(self):
        manifest = manifest_from_dict(good()).manifest
        blocking, _ = api_compatibility(manifest, "draft")
        self.assertIs(blocking.code, ProblemCode.API_VERSION_MISMATCH)


if __name__ == "__main__":
    unittest.main()
