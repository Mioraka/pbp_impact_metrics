import csv
import json
import tempfile
import unittest
from pathlib import Path

from combine_results import combine
from pull_on_off import IDENTIFIERS, write_csv


class CombineTests(unittest.TestCase):
    def fixture(self, source):
        folder = source / "2000"
        row = dict.fromkeys(IDENTIFIERS, "")
        row.update(league="nba", season="2000-01", season_type="Regular Season",
                   player_id="1", team_id="2", player_name="Example", metric_count="1",
                   example_on="1.000", example_off="0.900", example_diff="0.100")
        write_csv(folder / "player_season_on_off.csv", [row], list(row))
        inventory = {key: row[key] for key in IDENTIFIERS if key not in ("metric_count", "retrieved_at_utc", "source_url")}
        write_csv(folder / "player_inventory.csv", [inventory], list(inventory))
        query = {"kind": "onoff", "status": "ok", "url": "https://api.pbpstats.com/example"}
        write_csv(folder / "queries.csv", [query], list(query))
        write_csv(folder / "failures.csv", [], list(query))
        write_csv(folder / "coverage.csv", [{"season": "2000-01", "missing_rows": "0"}], ["season", "missing_rows"])
        metric = {"api_stat": "Example", "on_column": "example_on", "off_column": "example_off", "diff_column": "example_diff"}
        write_csv(folder / "metric_dictionary.csv", [metric], list(metric))
        summary = {"complete": True, "start_year": 2000, "end_year": 2000,
                   "season_types": ["Regular Season", "Playoffs", "PlayIn"],
                   "expected_rows": 1, "downloaded_rows": 1, "started_at_utc": "2026-11-09T00:00:00+00:00"}
        (folder / "run_summary.json").write_text(json.dumps(summary))
        return folder

    def test_combines_complete_season_and_preserves_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root / "parts")
            combine(root / "parts", root / "data", 2000, 2000)
            with (root / "data/player_season_on_off.csv").open() as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows[0]["example_diff"], "0.100")
            self.assertTrue(json.loads((root / "data/run_summary.json").read_text())["complete"])
            self.assertTrue((root / "data/checksums.csv").exists())

    def test_rejects_inventory_mismatch_before_writing_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = self.fixture(root / "parts")
            path = folder / "player_inventory.csv"
            path.write_text(path.read_text().replace(",1,", ",999,"))
            with self.assertRaisesRegex(ValueError, "Key mismatch"):
                combine(root / "parts", root / "data", 2000, 2000)
            self.assertFalse((root / "data/player_season_on_off.csv").exists())

    def test_rejects_incomplete_season(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = self.fixture(root / "parts")
            path = folder / "run_summary.json"
            data = json.loads(path.read_text())
            data["complete"] = False
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "incomplete season"):
                combine(root / "parts", root / "data", 2000, 2000)


if __name__ == "__main__":
    unittest.main()
