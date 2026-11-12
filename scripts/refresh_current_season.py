#!/usr/bin/env python3
"""Refresh current-season inputs in isolation and publish only a validated master."""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from build_advanced_master import read_metric_definitions
from join_on_off_volume_to_master import run as join_volume

ROOT = Path(__file__).resolve().parents[1]


def current_season(now=None):
    now = now or datetime.now(ZoneInfo('America/Toronto'))
    year = now.year if now.month >= 10 else now.year - 1
    return f'{year}-{(year + 1) % 100:02d}'


def combine_volume(existing, fresh, season):
    keys = ['Season', 'SeasonType', 'Player_id', 'Team_id']
    if not fresh['Season'].eq(season).all() or fresh.empty:
        raise ValueError('Fresh volume must contain exactly the requested season')
    return pd.concat([existing.loc[~existing['Season'].eq(season)], fresh], ignore_index=True)


def refresh(data, season):
    definitions = ROOT / 'data/metric_definitions.csv'
    metric_names = {r['metrics'] for r in read_metric_definitions(definitions)}
    with tempfile.TemporaryDirectory(prefix='daily-pbp-') as directory:
        stage = Path(directory)
        for name in ('player_list.csv', 'player_advanced_metrics.csv', 'league_season_averages.csv'):
            shutil.copy2(data / name, stage / name)
        # Incremental master validation expects the original 89 metrics only.
        old_master = pd.read_csv(data / 'player_league_master.csv', low_memory=False)
        old_master.loc[old_master['metrics'].isin(metric_names)].to_csv(stage / 'player_league_master.csv', index=False)
        def execute(script, *arguments):
            subprocess.run([sys.executable, '-u', str(ROOT / 'scripts' / script), *map(str, arguments)], check=True)
        execute('append_missing_master.py', '--player-list', stage / 'player_list.csv',
                '--metric-definitions', definitions, '--player-output', stage / 'player_advanced_metrics.csv',
                '--league-output', stage / 'league_season_averages.csv', '--master-output', stage / 'player_league_master.csv',
                '--end-year', season[:4], '--refresh-season', season, '--refresh-league-season', season,
                '--cache', stage / 'advanced-cache')
        master = pd.read_csv(stage / 'player_league_master.csv', low_memory=False)
        subset = master.loc[master['Season'].eq(season)]
        if subset.empty:
            raise ValueError(f'No listed players for {season}; update data/player_list.csv')
        subset.to_csv(stage / 'current_master.csv', index=False)
        # A new cache and progress file force fresh volume requests on every invocation.
        execute('pull_on_off_volume.py', '--input-master', stage / 'current_master.csv',
                '--output-csv', stage / 'fresh_volume.csv', '--cache', stage / 'volume-cache',
                '--progress', stage / 'volume-progress.json', '--batch-limit', '100000')
        volume = combine_volume(pd.read_csv(data / 'player_on_off_volume.csv'),
                                pd.read_csv(stage / 'fresh_volume.csv'), season)
        volume.to_csv(stage / 'player_on_off_volume.csv', index=False)
        summary = join_volume(stage / 'player_league_master.csv', stage / 'player_on_off_volume.csv',
                              stage / 'player_league_master.csv')
        result = pd.read_csv(stage / 'player_league_master.csv', low_memory=False)
        if result['PlayerValue'].isna().any() or result.duplicated(['source_row', 'metrics', 'Court Status']).any():
            raise ValueError('Final master contains missing values or duplicate metric/status rows')
        manifest = json.loads((stage / 'run_manifest.json').read_text())
        manifest.update(master_rows=len(result), base_metric_count=89,
                        metric_count=summary['output_metrics'], refreshed_season=season,
                        volume_refresh_complete=True, volume_join=summary)
        (stage / 'run_manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        # GitHub commits the complete set in one commit, after this process succeeds.
        for name in ('player_advanced_metrics.csv', 'league_season_averages.csv',
                     'player_on_off_volume.csv', 'player_league_master.csv', 'run_manifest.json', 'request_log.json'):
            shutil.copy2(stage / name, data / name)
        print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--season', default=current_season())
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    args = parser.parse_args()
    refresh(args.data_dir, args.season)
