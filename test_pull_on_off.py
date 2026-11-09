import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pull_on_off import Client, metric_slug, validate_payload


def payload():
    return {"results": [
        {"Stat": name, "On": "100.001", "Off": "98.000", "On-Off": "2.001"}
        for name in ("Pts per 100 Possessions", "Pts per 100 Possessions - Defense")
    ]}


class PullTests(unittest.TestCase):
    def test_cap_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "500"):
            validate_payload("players", {"multi_row_table_data": [{"EntityId": "1", "Name": "A"}] * 500})

    def test_incomplete_or_duplicate_metrics_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_payload("onoff", {"results": []})
        data = payload()
        data["results"].append(data["results"][0])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            validate_payload("onoff", data)

    def test_nonfinite_values_are_rejected(self):
        data = payload()
        data["results"][0]["On"] = "NaN"
        with self.assertRaisesRegex(ValueError, "Non-finite"):
            validate_payload("onoff", data)

    @patch("pull_on_off.time.sleep")
    def test_retry_then_checkpoint_resume(self, sleep):
        with tempfile.TemporaryDirectory() as directory:
            client = Client(Path(directory), attempts=3)
            failed = Mock(status_code=503, headers={})
            success = Mock(status_code=200)
            success.json.return_value = payload()
            session = Mock()
            session.get.side_effect = [failed, success]
            client.local.session = session
            params = {"Season": "2026-27", "SeasonType": "Regular Season", "TeamId": "1", "PlayerId": "2"}
            data, info = client.get("onoff", params)
            self.assertEqual(data, payload())
            self.assertEqual(info["attempts"], 2)
            self.assertEqual(session.get.call_count, 2)
            self.assertEqual(sleep.call_count, 1)
            data, info = client.get("onoff", params)
            self.assertTrue(info["cache_hit"])
            self.assertEqual(session.get.call_count, 2)

    @patch("pull_on_off.time.sleep")
    def test_exhaustion_is_recorded(self, sleep):
        with tempfile.TemporaryDirectory() as directory:
            client = Client(Path(directory), attempts=2)
            client.local.session = Mock()
            client.local.session.get.return_value = Mock(status_code=500, headers={})
            with self.assertRaisesRegex(RuntimeError, "Exhausted"):
                client.get("teams", {"Season": "2026-27", "SeasonType": "Regular Season", "Type": "Team"})
            self.assertEqual(len(client.failures), 1)
            self.assertEqual(client.failures[0]["attempts"], 2)

    def test_metric_column_mapping(self):
        self.assertEqual(metric_slug("eFG% - Defense"), "efg_pct_defense")


if __name__ == "__main__":
    unittest.main()
