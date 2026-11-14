import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import fill_league_volume as fill

class LeagueVolumeTests(unittest.TestCase):
    def test_export_keeps_all_returned_players_and_leaves_cache_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); cache=root/'cache'; output=root/'raw.csv'
            job=('2000-01','1610612737','FGA')
            path=fill.cache_path(cache,*job);path.parent.mkdir(parents=True)
            payload={'results':[dict(Name=n,On=100,Off=50,**{'On-Off':50},MinutesOn=10,MinutesOff=5) for n in ('Player One','Player One')]}
            path.write_text(json.dumps(payload));original=path.read_bytes()
            with patch.object(fill,'CACHE',cache),patch.object(fill,'OUTPUT',output):
                self.assertTrue(fill.valid(job))
                self.assertFalse(fill.valid(('2000-01','1610612737','FTA')))
                self.assertEqual(fill.export([{'Season':job[0],'Team_id':job[1],'Team':'ATL'}],[job]),2)
            with output.open() as handle:
                rows=list(csv.DictReader(handle))
            self.assertEqual({r['PlayerName'] for r in rows},{'Player One'})
            self.assertEqual([r['ResponseRow'] for r in rows], ['1','2'])
            self.assertEqual(path.read_bytes(),original)
            self.assertEqual(rows[0]['MinutesOff'],'5')

    def test_invalid_cache_is_not_treated_as_complete(self):
        with tempfile.TemporaryDirectory() as d:
            cache=Path(d);job=('2000-01','1610612737','FGA')
            path=fill.cache_path(cache,*job);path.parent.mkdir(parents=True);path.write_text('{"results":[]}')
            with patch.object(fill,'CACHE',cache):self.assertFalse(fill.valid(job))

if __name__=='__main__':unittest.main()
