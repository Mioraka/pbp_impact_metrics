import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from refresh_current_season import current_season, combine_volume

class DailyRefreshTests(unittest.TestCase):
    def test_season_rollover(self):
        tz = ZoneInfo('America/Toronto')
        self.assertEqual(current_season(datetime(2026, 9, 30, tzinfo=tz)), '2025-26')
        self.assertEqual(current_season(datetime(2026, 10, 1, tzinfo=tz)), '2026-27')

    def test_replace_current_volume_preserves_history(self):
        old = pd.DataFrame({'Season': ['2025-26', '2026-27'], 'FGA_ON': [100, 200]})
        fresh = pd.DataFrame({'Season': ['2026-27'], 'FGA_ON': [300]})
        result = combine_volume(old, fresh, '2026-27')
        self.assertEqual(result['FGA_ON'].tolist(), [100, 300])

    def test_reject_wrong_or_empty_refresh(self):
        old = pd.DataFrame({'Season': ['2025-26']})
        with self.assertRaises(ValueError):
            combine_volume(old, old, '2026-27')
        with self.assertRaises(ValueError):
            combine_volume(old, old.iloc[:0], '2026-27')
