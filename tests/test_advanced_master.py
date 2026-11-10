import csv
import tempfile
import unittest
from pathlib import Path

from scripts.build_advanced_master import (
    calculate_metric,
    build_master,
    extract_totals_row,
    read_metric_definitions,
    season_code,
    validate_player_payload,
)


ROOT = Path(__file__).resolve().parents[1]


class AdvancedMasterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.definitions = read_metric_definitions(ROOT / "data/metric_definitions.csv")
        cls.expected = {row["metrics"] for row in cls.definitions}

    def test_metric_contract_is_89_unique_metrics(self):
        self.assertEqual(len(self.expected), 89)

    def test_season_normalization(self):
        self.assertEqual(season_code("2000-2001"), "2000-01")
        self.assertEqual(season_code("2025-26"), "2025-26")
        with self.assertRaises(ValueError):
            season_code("2025")

    def test_player_payload_requires_all_89_metrics(self):
        rows = [{"Stat": name, "On": "1", "Off": "2", "On-Off": "-1"}
                for name in self.expected]
        self.assertEqual(len(validate_player_payload({"results": rows}, self.expected)), 89)
        with self.assertRaisesRegex(ValueError, "contract mismatch"):
            validate_player_payload({"results": rows[:-1]}, self.expected)

    def test_player_payload_rejects_duplicate_metric(self):
        row = {"Stat": "duplicate", "On": 1, "Off": 2, "On-Off": -1}
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_player_payload({"results": [row, row]}, {"duplicate"})

    def test_league_raw_row_and_formula(self):
        payload = {"single_row_table_data": {"Points": 200, "OffPoss": 100,
                                               "SecondsPerPossOff": 15}}
        totals = extract_totals_row(payload)
        formula = {"metrics": "sample", "api_stat_id": "100*Points/OffPoss"}
        self.assertEqual(calculate_metric(formula, totals, {}), 200)
        seconds = {"metrics": "Seconds Per Possession - Offense", "api_stat_id": "SecondsPerPoss"}
        self.assertEqual(calculate_metric(seconds, totals, {}), 15)

    def test_master_join_has_player_and_league_values(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            player_list = root / "players.csv"
            stats = root / "stats.csv"
            league = root / "league.csv"
            master = root / "master.csv"
            player_fields = ["SEASON_YEAR", "SEASON", "Season_Player", "PLAYER_NAME", "Player_id",
                             "Team_id", "ALL_STAR", "ALL_NBA", "ALL_DEF", "MVP", "DPOY", "Any Award"]
            player = {"SEASON_YEAR": "2001", "SEASON": "2000-2001", "Season_Player": "Test Player - 2001",
                      "PLAYER_NAME": "Test Player", "Player_id": "1", "Team_id": "2",
                      "ALL_STAR": "1", "ALL_NBA": "0", "ALL_DEF": "0", "MVP": "0", "DPOY": "0", "Any Award": "1"}
            with player_list.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=player_fields); writer.writeheader(); writer.writerow(player)
            with stats.open("w", newline="", encoding="utf-8") as handle:
                fields = ["source_row", *player_fields, "metrics", "Court Status", "Value"]
                writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
                for index, definition in enumerate(self.definitions):
                    for status in ("On", "Off", "On-Off"):
                        writer.writerow({"source_row": 1, **player, "metrics": definition["metrics"],
                                         "Court Status": status, "Value": index + 1})
            with league.open("w", newline="", encoding="utf-8") as handle:
                fields = ["Season", "SeasonType", "metrics", "Value", "api_stat_id", "retrieved_at_utc"]
                writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
                for definition in self.definitions:
                    writer.writerow({"Season": "2000-01", "SeasonType": "Regular Season",
                                     "metrics": definition["metrics"], "Value": 10,
                                     "api_stat_id": definition["api_stat_id"], "retrieved_at_utc": "test"})
            rows = build_master(player_list, stats, league, master, self.definitions)
            self.assertEqual(len(rows), 89 * 3)
            self.assertEqual(rows[0]["PlayerValue"], 1.0)
            self.assertEqual(rows[0]["LeagueAverage"], 10.0)
            self.assertEqual(rows[0]["PlayerMinusLeague"], -9.0)


if __name__ == "__main__":
    unittest.main()
