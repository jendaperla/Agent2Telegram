"""Public source is English; Czech is restricted to explicit localized strings.

Allow exact string literals at named paths, never entire files or comments.
Unicode transport tests use language-neutral text rather than private examples.
"""
import ast
import io
import os
from pathlib import Path
import subprocess
import tempfile
import tokenize
import unicodedata
import unittest


LOCALIZATIONS = {
    "agent2telegram/inbound.py": {"uživatel", "bez názvu"},
    "agent2telegram/notices.py": {
        "⚡ Zprávu mám. Dostanu se k ní, jakmile dokončím rozpracovaný úkol.",
        "🔄 Most je znovu připojený.",
    },
    "agent2telegram/background.py": {"🤖 Na pozadí", "+{n} další", "⏳ pracuje…"},
    "tests/test_notices.py": {"🔄 Most je znovu připojený.", "Zprávu mám"},
    "tests/test_background_progress.py": {"Na pozadí: Build the thing · 3:20"},
    "tests/test_inbound_sender_label.py": {'[Od: uživatel · skupina "bez názvu"]'},
}
# The allowlist itself contains the same reviewed literals, with no broader exemption.
LOCALIZATIONS["tests/test_public_language.py"] = set().union(*LOCALIZATIONS.values())
ACCENTS = set("".join(chr(n) for n in (
    225, 269, 271, 233, 283, 237, 328, 243, 345, 353, 357, 250, 367, 253, 382
)))
ACCENTS |= {c.upper() for c in ACCENTS}


def violations(path, text):
    text = unicodedata.normalize("NFC", text)
    lines = text.splitlines(keepends=True)
    allowed = LOCALIZATIONS.get(path, set())
    if path.endswith(".py") and allowed:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type != tokenize.STRING:
                continue
            try:
                value = ast.literal_eval(token.string)
            except (ValueError, SyntaxError):
                continue
            if not isinstance(value, str) or value not in allowed:
                continue
            start, end = token.start, token.end
            for row in range(start[0], end[0] + 1):
                a = start[1] if row == start[0] else 0
                b = end[1] if row == end[0] else len(lines[row - 1])
                line = lines[row - 1]
                lines[row - 1] = line[:a] + " " * (b - a) + line[b:]
    return [f"{path}:{n}" for n, line in enumerate(lines, 1) if set(line) & ACCENTS]


def source_files(root):
    # git archive / GitHub source downloads have no repository metadata. Still
    # scan their source; do not skip the guard or accidentally scan a parent repo.
    if (root / ".git").exists():
        return sorted(set(subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root
        ).decode().split("\0")) - {""})
    names = []
    for directory, folders, files in os.walk(root):
        folders[:] = [name for name in folders if name not in (
            ".git", "build", "dist", "__pycache__", ".venv", "venv", ".pytest_cache"
        ) and not name.endswith(".egg-info")]
        names.extend(str((Path(directory) / name).relative_to(root)) for name in files)
    return sorted(names)


class PublicLanguageTests(unittest.TestCase):
    def test_source_archive_is_scanned_without_git_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text(chr(382), encoding="utf-8")
            (root / "build").mkdir()
            (root / "build/generated.py").write_text(chr(382), encoding="utf-8")
            names = source_files(root)
            self.assertEqual(names, ["README.md"])
            self.assertTrue(violations(names[0], (root / names[0]).read_text()))

    def test_repository_only_contains_explicit_czech_localization(self):
        root = Path(__file__).resolve().parents[1]
        names = source_files(root)
        failures = []
        for name in sorted(set(names) - {""}):
            path = root / name
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                # Binary assets have no source text to localize.
                continue
            failures.extend(violations(name, text))
        self.assertEqual(failures, [], "Unlocalized Czech characters: " + ", ".join(failures))

    def test_comments_and_unlisted_literals_cannot_use_the_allowlist(self):
        path = "agent2telegram/inbound.py"
        value = sorted(LOCALIZATIONS[path])[0]
        self.assertEqual(violations(path, repr(value)), [])
        self.assertTrue(violations(path, "# " + repr(value)))
        self.assertTrue(violations("other.py", repr(value)))
        self.assertTrue(violations(path, repr(value + " extra")))
        self.assertTrue(violations("README.md", chr(382)))
        self.assertTrue(violations("README.md", "z" + chr(780)))
