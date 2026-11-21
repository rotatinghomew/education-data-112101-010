import csv
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class FixtureIntegrityTests(unittest.TestCase):
    def test_fixture_csv_files_are_readable_and_have_rows(self):
        files = sorted((ROOT / "data").glob("*.csv"))
        self.assertGreaterEqual(len(files), 3)
        for path in files:
            with self.subTest(file=path.name), path.open(encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                self.assertTrue(reader.fieldnames)
                rows = list(reader)
                self.assertTrue(rows)
                self.assertTrue(all(set(row) == set(reader.fieldnames) for row in rows))

if __name__ == "__main__":
    unittest.main()
