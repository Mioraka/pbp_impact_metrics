#!/usr/bin/env python3
"""Run bounded collection segments and publish durable GitHub checkpoints."""
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
STATE = Path('state/on_off_volume')
CACHE = STATE / 'cache'
PROGRESS = CACHE / 'progress.json'
OUTPUT = Path('data/player_on_off_volume.csv')

def initialize():
    if not PROGRESS.exists():
        raw = base64.b64decode((STATE / 'seed.tar.gz.b64').read_text())
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
            archive.extractall(STATE, filter='data')

def publish(status, error=''):
    progress = json.loads(PROGRESS.read_text())
    now = datetime.now(timezone.utc).isoformat()
    progress.update(status=status, updated_at=now, last_error=error,
                    run_url=os.environ.get('RUN_URL', 'local'))
    (STATE / 'status.json').write_text(json.dumps(progress, indent=2)+'\n')
    done, total = progress['next_job'], progress['total_jobs']
    body = (f'# On/off volume collection\n\nStatus: **{status}**\n\n'
            f'Requests: **{done:,}/{total:,} ({done/total:.1%})**\n\n'
            f'Completed batches: **{progress["completed_batches"]}**\n\n'
            f'Next batch size: **{progress["current_batch_size"]}**; retry wait: **{progress.get("wait_seconds", 0)} seconds**.\n\n'
            f'Updated (UTC): {now}\n\n[Workflow run]({progress["run_url"]})\n\n')
    if error:
        body += f'Last error: {error}\n\n'
    body += ('Each successful batch increases concurrency by one, up to 9. Wait increases by 5 seconds after failure and decreases by 5 seconds after success, to a floor of 0. The first failed pass holds '
             'concurrency; a second failed pass lowers it by one. Successful responses are retained.\n\n'
             'The workflow checkpoints every 10 completed batches (and on errors or time limits). '
             'Live logs show every batch and request failure. Scheduled runs resume from the checkpoint.\n')
    (STATE / 'STATUS.md').write_text(body)
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as f:
            f.write(body+'\n---\n')
    print(f'STATUS {status}: {done}/{total} requests; {progress["completed_batches"]} batches', flush=True)
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        subprocess.run(['git','add',str(STATE),str(OUTPUT.parent)],check=True)
        if subprocess.run(['git','diff','--cached','--quiet']).returncode != 0:
            subprocess.run(['git','commit','-m',f'Checkpoint on/off volume: {done}/{total} ({status}) [skip ci]'],check=True)
            subprocess.run(['git','push','origin','HEAD:main'],check=True)

def main():
    initialize()
    if OUTPUT.exists() and json.loads(PROGRESS.read_text())['next_job'] == json.loads(PROGRESS.read_text())['total_jobs']:
        print('Already complete; no requests needed.', flush=True)
        return
    deadline = time.monotonic() + 45*60
    publish('running')
    while time.monotonic() < deadline:
        remaining = deadline-time.monotonic()
        if remaining < 15:
            break
        # End segments on a multiple of 10 completed batches.
        count = json.loads(PROGRESS.read_text())['completed_batches']
        batch_limit = 10 - count % 10
        command = [sys.executable,'-u','scripts/pull_on_off_volume.py',
                   '--input-master',str(STATE/'input.csv'),'--output-csv',str(OUTPUT),
                   '--cache',str(CACHE),'--progress',str(PROGRESS),
                   '--batch-limit',str(batch_limit),'--batch-size','4','--initial-wait','5','--workers','9']
        try:
            result = subprocess.run(command, timeout=min(600,remaining))
        except subprocess.TimeoutExpired:
            publish('scheduled to resume', 'Segment reached its time limit; cached responses preserved.')
            return
        if result.returncode:
            publish('retry needed', f'Collector exited {result.returncode}; see request errors in run logs. Next scheduled run resumes.')
            raise SystemExit(result.returncode)
        progress = json.loads(PROGRESS.read_text())
        if progress['next_job'] == progress['total_jobs']:
            if not OUTPUT.exists():
                publish('retry needed', 'All requests cached but output is missing.')
                raise SystemExit(1)
            publish('complete')
            return
        publish('running')
    publish('scheduled to resume')

if __name__ == '__main__':
    main()
