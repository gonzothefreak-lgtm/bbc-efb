"""Static checks for publish.ps1.

PowerShell is not available to the offline suite, so these tests check what can
be checked from Python: the script stays ASCII with balanced delimiters, only
calls functions and cmdlets that exist, keeps its safety steps in the right
order, and its credential and path rules (which use regex syntax shared by .NET
and Python) give the intended matches. The credential rules are also run over
the working tree, so a routine publish is not blocked by the repository itself.

Token-shaped test strings are assembled at run time so this file never matches
the rules it tests.

PowerShell is not installed on the development machine, so publish.ps1 itself
has never been executed by these tests: the temporary-index flow, ZipArchive
reading and the Git calls have no runtime test here.
"""

import io
import os
import re
import struct
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "publish.ps1"
TEXT = SCRIPT.read_text(encoding="ascii")

KNOWN_CMDLETS = {
    "Add-Type", "Copy-Item", "Get-Command", "Get-Location", "Move-Item", "New-Object", "Out-Null",
    "Pop-Location", "Push-Location", "Remove-Item", "Select-Object", "Test-Path", "Where-Object",
    "Write-Output",
}


def ps_single_quoted(name):
    match = re.search(r"^\$" + name + r" = '((?:[^']|'')*)'$", TEXT, re.MULTILINE)
    if match is None:
        raise AssertionError(f"${name} is not a single-quoted assignment in publish.ps1")
    return match.group(1).replace("''", "'")


TOKEN = re.compile(ps_single_quoted("tokenPattern"))
PRIVATE_KEY = re.compile(ps_single_quoted("privateKeyPattern"))
ASSIGNMENT = re.compile(ps_single_quoted("assignmentPattern"), re.IGNORECASE)
SENSITIVE_PATH = re.compile(ps_single_quoted("sensitivePath"), re.IGNORECASE)
ALLOWED_EMAIL = re.compile(ps_single_quoted("allowedEmailPattern"), re.IGNORECASE)


def credential_hits(text):
    return [rule for rule in (TOKEN, PRIVATE_KEY, ASSIGNMENT) if rule.search(text)]


def ps_size(name):
    match = re.search(r"^\$" + name + r" = (\d+)MB\b", TEXT, re.MULTILINE)
    if match is None:
        raise AssertionError(f"${name} is not an 'N MB' assignment in publish.ps1")
    return int(match.group(1)) * 1024 * 1024


MAX_BLOB_BYTES = ps_size("maxBlobBytes")
MAX_OPAQUE_BYTES = ps_size("maxOpaqueBinaryBytes")
MAX_ARCHIVE_DEPTH = int(re.search(r"^\$maxArchiveDepth = (\d+)", TEXT, re.MULTILINE).group(1))
OPAQUE_ARCHIVES = (b"\x1f\x8b", b"7z\xbc\xaf\x27\x1c", b"Rar!", b"\xfd7zXZ", b"BZh", b"\x28\xb5\x2f\xfd", b"MSCF")


# --- What publish.ps1 would stage ------------------------------------------
#
# "git add --all" stages tracked files (even ones .gitignore matches, such as a
# tracked *.zip) and untracked files that .gitignore does not exclude. Git can't
# be run here (the offline guard blocks subprocesses), so the index is read
# directly and .gitignore is applied with Git's matching rules.


def git_dir():
    dot = ROOT / ".git"
    if dot.is_dir():
        return dot
    if dot.is_file():
        text = dot.read_text(encoding="utf-8").strip()
        if text.startswith("gitdir:"):
            path = Path(text.split(":", 1)[1].strip())
            return path if path.is_absolute() else (ROOT / path).resolve()
    return None


def tracked_paths():
    """Paths recorded in the Git index (formats 2 and 3), or None when unreadable."""

    folder = git_dir()
    if folder is None:
        return None
    try:
        data = (folder / "index").read_bytes()
    except OSError:
        return None
    if data[:4] != b"DIRC":
        return None
    version, count = struct.unpack(">II", data[4:12])
    if version not in (2, 3):
        return None
    paths, position = set(), 12
    for _ in range(count):
        flags = struct.unpack(">H", data[position + 60:position + 62])[0]
        start = position + 62 + (2 if version == 3 and flags & 0x4000 else 0)
        end = data.index(b"\0", start)
        paths.add(data[start:end].decode("utf-8"))
        # Each entry is padded with 1-8 NUL bytes to a multiple of eight.
        position += (end - position + 8) // 8 * 8
    return paths


def glob_regex(pattern):
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[" and "]" in pattern[i + 1:]:
            close = pattern.index("]", i + 1)
            out.append("[" + pattern[i + 1:close].replace("!", "^", 1) + "]")
            i = close + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def gitignore_rules(text):
    rules = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        negate = line.startswith("!")
        line = line[1:] if negate else line
        directory_only = line.endswith("/")
        line = line.rstrip("/")
        anchored = "/" in line
        rules.append((glob_regex(line.lstrip("/")), negate, directory_only, anchored))
    return rules


def is_ignored(relative, rules):
    """True when .gitignore excludes the file or one of its folders (last rule wins)."""

    parts = relative.split("/")
    for depth in range(1, len(parts) + 1):
        is_folder = depth < len(parts)
        ignored = False
        for regex, negate, directory_only, anchored in rules:
            if directory_only and not is_folder:
                continue
            if regex.fullmatch("/".join(parts[:depth]) if anchored else parts[depth - 1]):
                ignored = not negate
        if ignored:
            return True
    return False


def publish_candidates():
    """Relative paths that "git add --all" would stage in this working tree."""

    rules = gitignore_rules((ROOT / ".gitignore").read_text(encoding="utf-8"))
    tracked = tracked_paths() or set()
    found = []
    for folder, folders, files in os.walk(ROOT):
        folders[:] = sorted(d for d in folders if d != ".git")
        for name in sorted(files):
            relative = (Path(folder) / name).relative_to(ROOT).as_posix()
            if relative in tracked or not is_ignored(relative, rules):
                found.append(relative)
    return found


# --- The script's content scan, in Python ----------------------------------


def text_encoding(data):
    """Mirror Get-TextEncoding: the codec for text, or None for binary data."""

    if data.startswith(b"\xff\xfe"):
        return "utf-16-le"
    if data.startswith(b"\xfe\xff"):
        return "utf-16-be"
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    sample = data[:8192]
    even = sum(1 for i in range(0, len(sample), 2) if sample[i] == 0)
    odd = sum(1 for i in range(1, len(sample), 2) if sample[i] == 0)
    if even == 0 and odd == 0:
        return "utf-8"
    half = max(1, len(sample) // 2)
    if odd / half > 0.3 and even / half < 0.05:
        return "utf-16-le"
    if even / half > 0.3 and odd / half < 0.05:
        return "utf-16-be"
    return None


def content_problems(data, location, depth=0):
    """Mirror Get-ContentProblems: credential hits and files the script refuses."""

    if len(data) > MAX_BLOB_BYTES:
        return [f"{location}: too large to scan"]
    if data.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        if depth >= MAX_ARCHIVE_DEPTH:
            return [f"{location}: archive nested too deeply"]
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            return [f"{location}: unreadable ZIP"]
        problems = []
        with archive:
            for info in archive.infolist():
                entry = f"{location}!{info.filename}"
                if SENSITIVE_PATH.search(info.filename.replace("\\", "/")):
                    problems.append(f"{entry}: sensitive path inside an archive")
                if not info.is_dir():
                    problems += content_problems(archive.read(info), entry, depth + 1)
        return problems
    if data.startswith(OPAQUE_ARCHIVES):
        return [f"{location}: compressed archive the script cannot inspect"]
    encoding = text_encoding(data)
    if encoding is None:
        if len(data) > MAX_OPAQUE_BYTES:
            return [f"{location}: opaque binary over the byte-scan limit"]
        encoding = "latin-1"
    text = data.decode(encoding, errors="replace")
    return [f"{location}: {rule.pattern[:30]}" for rule in credential_hits(text)]


def code_without_strings_and_comments(text):
    """Return the script with strings and comments blanked, checking delimiters."""

    out = []
    pairs = {")": "(", "}": "{", "]": "["}

    def code(i, closer):
        stack = []
        while i < len(text):
            char = text[i]
            if char == "#":
                while i < len(text) and text[i] != "\n":
                    i += 1
                continue
            if char == "`":
                out.append("  ")
                i += 2
                continue
            if char == "'":
                i = single(i + 1)
                continue
            if char == '"':
                i = double(i + 1)
                continue
            if char in "({[":
                stack.append(char)
            elif char in ")}]":
                if not stack:
                    if char == closer:
                        return i
                    raise AssertionError(f"unbalanced {char!r} at offset {i}")
                if stack.pop() != pairs[char]:
                    raise AssertionError(f"mismatched {char!r} at offset {i}")
            out.append(char)
            i += 1
        if stack or closer:
            raise AssertionError(f"unclosed delimiters {stack} {closer!r}")
        return i

    def single(i):
        while True:
            if i >= len(text):
                raise AssertionError("unterminated single-quoted string")
            if text[i] == "'":
                if text[i + 1:i + 2] == "'":
                    i += 2
                    continue
                out.append("''")
                return i + 1
            i += 1

    def double(i):
        while True:
            if i >= len(text):
                raise AssertionError("unterminated double-quoted string")
            char = text[i]
            if char == "`":
                i += 2
            elif char == '"':
                out.append('""')
                return i + 1
            elif text.startswith("$(", i):
                i = code(i + 2, ")") + 1
            else:
                i += 1

    code(0, None)
    return "".join(out)


class PublishScriptStructureTests(unittest.TestCase):
    def test_script_is_ascii_without_here_strings_and_delimiters_balance(self):
        self.assertTrue(TEXT.isascii())
        self.assertNotRegex(TEXT, r"@['\"]\s*$", "here-strings are not covered by the delimiter check")
        code_without_strings_and_comments(TEXT)

    def test_every_called_command_is_defined_or_a_builtin_cmdlet(self):
        code = code_without_strings_and_comments(TEXT)
        defined = set(re.findall(r"^function ([A-Z][A-Za-z]+-[A-Za-z]+)", TEXT, re.MULTILINE))
        used = set(re.findall(r"(?<![\w$-])([A-Z][a-z]+-[A-Z][A-Za-z]+)\b", code))
        self.assertEqual(sorted(used - defined - KNOWN_CMDLETS), [])
        self.assertEqual(sorted(defined - used), [], "defined but never called")

    def test_validation_happens_before_the_real_index_changes(self):
        code = code_without_strings_and_comments(TEXT)
        main = code[code.index("Push-Location"):]
        order = [main.index(step) for step in (
            "fetch origin",
            "Test-AllowedEmail (Get-IdentityEmail",
            "$env:GIT_INDEX_FILE = $checkIndex",
            "Invoke-Git add --all",
            "Assert-CleanEntries -Entries @(Get-IndexEntries)",
            "Get-UnpublishedCommits",
            "Assert-UnpublishedIdentities",
            "Move-Item -LiteralPath $checkIndex -Destination $realIndex",
            "Invoke-Git commit",
            "push --set-upstream origin main",
        )]
        self.assertEqual(order, sorted(order))
        self.assertIn("Remove-Item Env:GIT_INDEX_FILE", main[main.index("finally"):])

    def test_every_identity_check_finishes_before_the_commit(self):
        # A failed identity check on an older local commit must not leave a new
        # commit behind: Assert-UnpublishedIdentities runs once, before committing.
        code = code_without_strings_and_comments(TEXT)
        main = code[code.index("Push-Location"):]
        self.assertEqual(main.count("Assert-UnpublishedIdentities"), 1)
        self.assertLess(main.index("Assert-UnpublishedIdentities"), main.index("Invoke-Git commit"))
        self.assertLess(main.index("Test-AllowedEmail (Get-IdentityEmail"), main.index("Invoke-Git commit"))

    def test_blob_size_is_checked_before_the_blob_is_read(self):
        code = code_without_strings_and_comments(TEXT)
        body = code[code.index("function Assert-CleanEntries"):code.index("function Get-IndexEntries")]
        self.assertLess(body.index("Get-GitBlobSize -ObjectId $entry.Blob) -gt $maxBlobBytes"),
                        body.index("Get-GitBlobBytes -ObjectId $entry.Blob"))
        self.assertIn("cat-file -s $ObjectId", TEXT)

    def test_header_describes_what_the_binary_scan_does_not_cover(self):
        header = TEXT[:TEXT.index("$ErrorActionPreference")]
        self.assertIn("PDF Flate streams", header)
        self.assertIn("8 MB", header)

    def test_only_unpublished_commits_are_rescanned(self):
        self.assertIn("rev-list HEAD --not $remoteBranchRef", TEXT)
        self.assertNotRegex(TEXT, r"foreach \(\$commit in \(Invoke-Git rev-list HEAD\)\)")
        self.assertNotIn("git grep", TEXT, "git grep -I skips binary and UTF-16 files")

    def test_paths_are_read_unquoted_and_matched_per_item(self):
        self.assertIn("core.quotepath=off", TEXT)
        for command in ("ls-files -s -z", "ls-tree -r -z"):
            self.assertIn(command, TEXT)
        # "$array -match $pattern" returns a Boolean for a single item.
        self.assertNotRegex(code_without_strings_and_comments(TEXT), r"\) -match \$sensitivePath")

    def test_native_error_preference_cannot_turn_expected_exit_codes_into_errors(self):
        self.assertIn("$PSNativeCommandUseErrorActionPreference = $false", TEXT)


class PublishRuleTests(unittest.TestCase):
    def test_ordinary_prose_and_slugs_are_not_credentials(self):
        prose = ("See risk-assessment-and-mitigation-register-for-the-whole-project.\n"
                 "Region asiapacificregion0000001 and ASIA region text.\n"
                 "https://example.org/how-to-ask-questions-in-the-community-forums-now\n"
                 "task-" + "list-" * 10 + "done, desk-" + "a" * 40 + "\n")
        self.assertEqual(credential_hits(prose), [])

    def test_provider_tokens_are_case_sensitive_and_stand_alone(self):
        digits = "Ab3" * 12
        tokens = ["s" + "k-proj-" + digits, "g" + "hp_" + "A1b2C3d4E5" * 4,
                  "github" + "_pat_" + "11ABCDE" * 4, "AK" + "IA" + "ABCDEFGH23456789"]
        for token in tokens:
            with self.subTest(token=token[:6]):
                self.assertTrue(TOKEN.search(f'key: {token}\n'))
                self.assertTrue(TOKEN.search(f'"{token}"'))
                self.assertFalse(TOKEN.search("x" + token), "a token glued to a word is prose")
        self.assertFalse(TOKEN.search("ak" + "ia" + "abcdefgh23456789"))
        self.assertTrue(PRIVATE_KEY.search("-----BEGIN " + "OPENSSH PRIVATE KEY-----"))

    def test_assignments_are_case_insensitive_and_skip_placeholders(self):
        value = "abcdefghijklmnop" + "qrstuvwxyz"
        self.assertTrue(ASSIGNMENT.search("pass" + 'word = "' + value + '"'))
        self.assertTrue(ASSIGNMENT.search("DB_PASS" + "WORD='" + value + "'"))
        self.assertTrue(ASSIGNMENT.search('"api' + '_key": "' + value + '"'))
        self.assertFalse(ASSIGNMENT.search("pass" + 'word = "your_password_example_value_here"'))
        self.assertFalse(ASSIGNMENT.search("pass" + 'word = "short"'))

    def test_sensitive_paths_match_inside_non_ascii_folders_and_archives(self):
        for path in ("notes/Jürgen/.env", "notes/Jürgen/build", "a/b/secret.PEM",
                     "reference/x/core_live_readonly_2026-11-24/RAW.md", "x/.venv/lib.py", "tool.exe"):
            with self.subTest(path=path):
                self.assertTrue(SENSITIVE_PATH.search(path))
        for path in ("sources/05_CORE_AND_SIMCONNECT.md", "bbc_core/simconnect_sdk.py", "README.md",
                     "tests/test_repo_publish.py", "reference/04_REFERENCE_ASSETS/Flight Details.png"):
            with self.subTest(path=path):
                self.assertFalse(SENSITIVE_PATH.search(path))

    def test_identity_rule_accepts_only_github_noreply_addresses(self):
        allowed = re.findall(r"'([^']+)'", re.search(r"^\$allowedEmails = @\((.*)\)$", TEXT, re.MULTILINE).group(1))
        self.assertIn("noreply@github.com", allowed)
        for email in ("12938411+someone@users.noreply.github.com", "someone@users.noreply.github.com"):
            self.assertTrue(ALLOWED_EMAIL.match(email), email)
        for email in ("someone@gmail.com", "someone@users.noreply.github.com.evil.example",
                      "@users.noreply.github.com"):
            self.assertFalse(ALLOWED_EMAIL.match(email), email)

    def test_files_a_publish_would_stage_pass_the_content_scan(self):
        # Only what "git add --all" would stage: gitignored local evidence (live
        # reports, development reviews, caches) is never published, so it must
        # not fail this test on the owner's workstation.
        hits = []
        for relative in publish_candidates():
            if SENSITIVE_PATH.search(relative):
                hits.append(f"{relative}: sensitive or generated path")
                continue
            hits += content_problems((ROOT / relative).read_bytes(), relative)
        self.assertEqual(hits, [])


class PublishCandidateTests(unittest.TestCase):
    """The Python stand-ins for Git's staging and the script's scanner."""

    RULES = gitignore_rules(
        "__pycache__/\n*.py[cod]\n/build/\n*.zip\n/reference/x/development reviews/\n"
        "/reference/x/test_artifacts/core_live_readonly_*/\n/reference/x/test_artifacts/*/unittest.txt\n")

    def test_gitignore_rules_follow_git_matching(self):
        for path in ("a/__pycache__/m.cpython-312.pyc", "m.pyc", "build/x.txt", "deep/x.zip",
                     "reference/x/development reviews/R3/README.md",
                     "reference/x/test_artifacts/core_live_readonly_2026/RAW.md",
                     "reference/x/test_artifacts/core_first/unittest.txt"):
            with self.subTest(path=path):
                self.assertTrue(is_ignored(path, self.RULES))
        for path in ("bbc_core/core.py", "sub/build/x.txt", "reference/x/test_artifacts/core_first/REPORT.md",
                     "reference/x/test_artifacts/unittest.txt", "notes/x.pyx"):
            with self.subTest(path=path):
                self.assertFalse(is_ignored(path, self.RULES))
        self.assertFalse(is_ignored("keep.zip", gitignore_rules("*.zip\n!keep.zip\n")))

    def test_real_gitignore_keeps_build_output_and_local_evidence_out(self):
        rules = gitignore_rules((ROOT / ".gitignore").read_text(encoding="utf-8"))
        for path in ("bbc_efb.egg-info/PKG-INFO", "build/wheel/bbc_efb-0.1.0a1-py3-none-any.whl", "dist/x/a.zip",
                     "bbc_core/__pycache__/core.cpython-312.pyc", "logs/bbc-efb.log",
                     "reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_live_readonly_2026-11-24/PMDG_READONLY.md",
                     "reference/01_CODEX_WORK_EVIDENCE/development reviews/R3.1/README.md"):
            with self.subTest(path=path):
                self.assertTrue(is_ignored(path, rules))
        for path in ("pyproject.toml", "bbc_recorder/service.py", "packaging/bbc-efb.spec", ".github/workflows/ci.yml",
                     "reference/01_CODEX_WORK_EVIDENCE/test_artifacts/core_first_offline_2026-11-24/REPORT.md"):
            with self.subTest(path=path):
                self.assertFalse(is_ignored(path, rules))

    def test_the_real_index_is_read(self):
        tracked = tracked_paths()
        if tracked is None:
            self.skipTest("no readable Git index (format 2 or 3) in this checkout")
        self.assertIn("README.md", tracked)
        self.assertIn(".gitignore", tracked)
        candidates = set(publish_candidates())
        self.assertLessEqual({p for p in tracked if (ROOT / p).is_file()}, candidates)
        self.assertFalse([p for p in candidates if "__pycache__" in p.split("/")])

    def test_scanner_decodes_utf16_and_opens_zip_entries(self):
        token = "g" + "hp_" + "A1b2C3d4E5" * 4
        utf16 = ("key: " + token + "\n").encode("utf-16-le")
        self.assertTrue(content_problems(b"\xff\xfe" + utf16, "bom.txt"))
        self.assertTrue(content_problems(utf16, "nobom.txt"))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", "<w:t>" + token + "</w:t>")
            archive.writestr("x/.env", "A=1")
        joined = "\n".join(content_problems(buffer.getvalue(), "doc.docx"))
        self.assertIn("doc.docx!word/document.xml", joined)
        self.assertIn("doc.docx!x/.env: sensitive path", joined)
        self.assertTrue(content_problems(b"\x1f\x8b" + b"\0" * 10, "x.tgz"))
        self.assertEqual(content_problems(b"plain prose\n", "ok.txt"), [])


if __name__ == "__main__":
    unittest.main()
