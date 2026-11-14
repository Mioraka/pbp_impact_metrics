#!/usr/bin/env python3
"""Resume league-wide raw on/off volume collection without replacing the player master."""
import argparse
import csv
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from pull_on_off_volume import STATS, cache_path, get_payload, validate_payload, write_progress

CACHE = Path('state/on_off_volume/cache')
STATE = Path('state/league_volume')
OUTPUT = Path('data/league_player_on_off_volume_raw.csv')


def inventory():
    rows = json.loads(Path('data/league_team_seasons.json').read_text())
    keys = [(r['Season'], r['Team_id']) for r in rows]
    if len(keys) != 806 or len(set(keys)) != len(keys):
        raise ValueError('Expected 806 distinct NBA regular-season team-seasons')
    return rows


def valid(job):
    try:
        validate_payload(json.loads(cache_path(CACHE, *job).read_text()))
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def export(rows, jobs):
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    team_names = {(r['Season'], r['Team_id']): r['Team'] for r in rows}
    columns = ['Season', 'SeasonType', 'Team_id', 'Team', 'PlayerName', 'Stat',
               'On', 'Off', 'OnOff', 'MinutesOn', 'MinutesOff']
    temporary = OUTPUT.with_suffix('.tmp')
    count = 0
    with temporary.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for job in jobs:
            if not valid(job):
                continue
            season, team, stat = job
            payload = json.loads(cache_path(CACHE, *job).read_text())
            names = [r['Name'] for r in payload['results']]
            if len(set(names)) != len(names):
                raise ValueError(f'Duplicate names within {job}')
            for r in payload['results']:
                writer.writerow(dict(Season=season, SeasonType='Regular Season', Team_id=team,
                    Team=team_names[(season, team)], PlayerName=r['Name'], Stat=stat,
                    On=r['On'], Off=r['Off'], OnOff=r['On-Off'],
                    MinutesOn=r['MinutesOn'], MinutesOff=r['MinutesOff']))
                count += 1
    temporary.replace(OUTPUT)
    return count


def checkpoint(rows, jobs, batch, wait, size, publish):
    complete_jobs = sum(valid(j) for j in jobs)
    complete_pairs = sum(all(valid((r['Season'], r['Team_id'], s)) for s in STATS) for r in rows)
    raw_rows = export(rows, jobs)
    status = dict(status='complete' if complete_jobs == len(jobs) else 'in_progress',
        updated_at_utc=datetime.now(timezone.utc).isoformat(), total_team_seasons=len(rows),
        complete_team_seasons=complete_pairs, total_requests=len(jobs), cached_responses=complete_jobs,
        remaining_requests=len(jobs)-complete_jobs, raw_rows=raw_rows,
        completed_batches=batch, wait_seconds=wait, batch_size=size)
    write_progress(STATE / 'status.json', status)
    print(json.dumps(status), flush=True)
    if publish:
        subprocess.run(['git', 'add', str(CACHE), str(STATE), str(OUTPUT)], check=True)
        if subprocess.run(['git', 'diff', '--cached', '--quiet']).returncode:
            subprocess.run(['git', 'commit', '-m', 'Checkpoint league-wide raw volume backfill [skip ci]'], check=True)
            subprocess.run(['git', 'push', 'origin', 'HEAD:main'], check=True)
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--max-batches', type=int, default=50)
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--plan-only', action='store_true')
    args = parser.parse_args()
    rows = inventory()
    jobs = [(r['Season'], r['Team_id'], s) for r in rows for s in STATS]
    pending = [j for j in jobs if not valid(j)]
    print(f'PLAN team_seasons={len(rows)} total_responses={len(jobs)} missing_requests={len(pending)}', flush=True)
    if args.plan_only:
        return
    prior = json.loads((STATE / 'status.json').read_text()) if (STATE / 'status.json').exists() else {}
    batch = int(prior.get('completed_batches', 0))
    size = min(9, max(1, int(prior.get('batch_size', 9))))
    wait = min(30, max(0, float(prior.get('wait_seconds', 0))))
    checkpoint(rows, jobs, batch, wait, size, args.publish)
    try:
        for _ in range(args.max_batches):
            if not pending:
                break
            current = pending[:size]
            retry = list(current)
            for attempt in range(7):
                failed = []
                with ThreadPoolExecutor(max_workers=size) as pool:
                    futures = {pool.submit(get_payload, *j, 30, 1, CACHE, 0): j for j in retry}
                    for future in as_completed(futures):
                        try:
                            future.result()
                        except Exception as exc:
                            failed.append(futures[future])
                            print(f'REQUEST_FAILURE {futures[future]}: {exc}', flush=True)
                if not failed:
                    break
                retry = failed
                wait = min(30, wait + 5)
                size = max(1, size-1)
                if attempt == 6:
                    raise RuntimeError(f'Failed after seven attempts: {failed}')
                time.sleep(wait)
            pending = pending[len(current):]
            batch += 1
            size = min(9, size+1)
            wait = max(0, wait-5)
            print(f'BATCH_COMPLETE batch={batch} remaining={len(pending)} batch_size={size}', flush=True)
            if batch % 10 == 0:
                checkpoint(rows, jobs, batch, wait, size, args.publish)
            if wait:
                time.sleep(wait)
    finally:
        checkpoint(rows, jobs, batch, wait, size, args.publish)


if __name__ == '__main__':
    main()
