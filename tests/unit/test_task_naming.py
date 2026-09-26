"""A task is named from what its provider resolved, never left as the link."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from engine.models import ResolvedItem  # noqa: E402
from engine.title_scorer import name_from_items  # noqa: E402

SHOW = "Spider-Man - The Animated Series (1994-1998) - 480p"


def item(name: str, folder: str = "") -> ResolvedItem:
    return ResolvedItem("transfer.it", "https://transfer.it/t/abc", name,
                        relative_path=f"{folder}/{name}" if folder else name)


class TaskNamingTests(unittest.TestCase):
    def test_one_file_is_named_after_the_file(self):
        self.assertEqual(name_from_items([item("S01E01.mkv", SHOW)]), "S01E01.mkv")

    def test_a_shared_folder_is_named_after_the_folder_and_selection(self):
        folder = f"{SHOW}/{SHOW}/Season 02"
        items = [item(f"S02E{n:02}.mkv", folder) for n in range(1, 4)]
        self.assertEqual(name_from_items(items), f"{SHOW} — Season 02")

    def test_a_whole_folder_is_named_after_the_folder(self):
        items = [item("a.mkv", f"{SHOW}/Season 01"), item("b.mkv", f"{SHOW}/Season 02")]
        self.assertEqual(name_from_items(items), SHOW)

    def test_archive_parts_are_named_after_the_package(self):
        items = [item(f"Blackwood.part{n}.rar") for n in range(1, 4)]
        self.assertEqual(name_from_items(items), "Blackwood")

    def test_episodes_without_folders_are_named_after_the_show(self):
        items = [item(f"Blackwood Show S01E0{n}.mkv") for n in range(1, 4)]
        self.assertEqual(name_from_items(items), "Blackwood Show")

    def test_unrelated_files_name_the_first_and_count_the_rest(self):
        items = [item("notes.txt"), item("photo.jpg"), item("song.mp3")]
        self.assertEqual(name_from_items(items), "notes.txt (+2 items)")

    def test_nothing_resolved_gives_no_name(self):
        self.assertEqual(name_from_items([]), "")


if __name__ == "__main__":
    unittest.main()
