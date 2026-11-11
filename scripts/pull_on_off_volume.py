#!/usr/bin/env python3
"""Pull PBPStats on/off attempt and possession counts for master player rows.

The PBPStats /get-on-off/{league}/stat endpoint returns one statistic for all
players on a team in a season. Successful responses are cached by team-season-
stat so an interrupted run can resume without repeating successful requests.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API = "https://api.pbpstats.com/get-on-off/nba/stat"
STATS = ("OffPoss", "DefPoss", "FGA", "FTA", "OpponentFGA", "OpponentFTA")
RETRYABLE = {429, 500, 502, 503, 504}
API_FIELDS = {"Name", "On", "Off", "On-Off", "MinutesOn", "MinutesOff"}


def norm_name(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]", "", text.lower())


def season_code(value: str) -> str:
    match = re.fullmatch(r"(\d{4})-(\d{2}|\d{4})", str(value).strip())
    if not match:
        raise ValueError(f"Invalid season: {value!r}")
    return f"{match.group(1)}-{match.group(2)[-2:]}"


def read_master(path: Path):
    source = {}
    shares = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            source_row = row["source_row"].strip()
            key = (season_code(row["SEASON"]), row["Team_id"].strip(), norm_name(row["PLAYER_NAME"]))
            if source_row not in source:
                source[source_row] = {
                    "source_row": source_row,
                    "SEASON": row["SEASON"],
                    "Season": season_code(row["SEASON"]),
                    "SeasonType": row["SeasonType"],
                    "PLAYER_NAME": row["PLAYER_NAME"],
                    "Player_id": row["Player_id"],
                    "Team_id": row["Team_id"],
                    "name_key": key,
                }
            if row["Court Status"] in ("On", "Off") and row["metrics"] in ("3PAr", "3PAr - Defense"):
                metric_key = "OPP_3PAr" if row["metrics"] == "3PAr - Defense" else "3PAr"
                shares.setdefault(source_row, {})[(metric_key, row["Court Status"])] = float(row["PlayerValue"])
    if not source:
        raise ValueError("Master CSV has no source player rows")
    return source, shares


def cache_path(cache: Path, season: str, team: str, stat: str) -> Path:
    return cache / season / team / f"{stat}.json"


def get_payload(season: str, team: str, stat: str, timeout: int, attempts: int,
                cache: Path, wait_seconds: float):
    saved = cache_path(cache, season, team, stat)
    if saved.exists():
        try:
            payload = json.loads(saved.read_text(encoding="utf-8"))
            validate_payload(payload)
            return payload, 0, "cache"
        except (OSError, ValueError, json.JSONDecodeError):
            saved.unlink(missing_ok=True)

    params = urlencode({"Season": season, "SeasonType": "Regular Season", "TeamId": team, "Stat": stat})
    url = f"{API}?{params}"
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            request = Request(url, headers={"User-Agent": "pbp-impact-stats-volume/1.0", "Accept": "application/json"})
            with urlopen(request, timeout=timeout) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                payload = json.loads(response.read())
            validate_payload(payload)
            saved.parent.mkdir(parents=True, exist_ok=True)
            temporary = saved.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, saved)
            return payload, attempt, "ok"
        except HTTPError as exc:
            last_error = f"HTTP {exc.code}: {exc.reason}"
            if exc.code not in RETRYABLE:
                break
        except (URLError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < attempts and wait_seconds > 0:
            time.sleep(wait_seconds)
    raise RuntimeError(f"{season}/{team}/{stat} failed after {attempts} attempts: {last_error}")


def validate_payload(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("Response has no results list")
    if not payload["results"]:
        raise ValueError("Response results are empty")
    for row in payload["results"]:
        if not isinstance(row, dict) or not API_FIELDS.issubset(row):
            raise ValueError("Response row is missing on/off or minutes fields")
        for field in ("On", "Off", "On-Off", "MinutesOn", "MinutesOff"):
            float(row[field])


def write_progress(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(temp, path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-master", type=Path, default=Path("data/player_league_master.csv"))
    parser.add_argument("--output-csv", type=Path, default=Path("data/player_on_off_volume.csv"))
    parser.add_argument("--cache", type=Path, default=Path(".cache/player-on-off-volume"))
    parser.add_argument("--progress", type=Path, default=Path(".cache/player-on-off-volume/progress.json"))
    parser.add_argument("--batch-size", type=int, default=9)
    parser.add_argument("--batch-limit", type=int, default=10, help="Maximum successful batches in this invocation")
    parser.add_argument("--workers", type=int, default=9)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--attempts", type=int, default=7)
    parser.add_argument("--initial-wait", type=float, default=5.0)
    args = parser.parse_args()
    if min(args.batch_size, args.workers, args.batch_limit, args.attempts) < 1:
        parser.error("batch-size, workers, batch-limit and attempts must be positive")

    source, shares = read_master(args.input_master)
    expected_by_pair = {}
    for record in source.values():
        season, team, name = record["name_key"]
        expected_by_pair.setdefault((season, team), set()).add(name)
    pairs = sorted(expected_by_pair)
    jobs = [(season, team, stat) for season, team in pairs for stat in STATS]

    state = {"next_job": 0, "completed_batches": 0, "wait_seconds": 0,
             "current_batch_size": args.batch_size}
    if args.progress.exists():
        prior = json.loads(args.progress.read_text(encoding="utf-8"))
        state.update(prior)
    start = int(state["next_job"])
    batch_no = int(state["completed_batches"])
    current_batch_size = int(state.get("current_batch_size", args.batch_size))
    failure_streak = int(state.get("failure_streak", 0))
    wait_seconds = float(state.get("wait_seconds", args.initial_wait))
    batch_in_invocation = 0
    failures_total = 0
    target_completed_batches = batch_no + args.batch_limit

    while start < len(jobs) and batch_no < target_completed_batches:
        batch = jobs[start:min(start + current_batch_size, len(jobs))]
        outcomes = {}
        print(f"BATCH_START number={batch_no+1} requests={len(batch)} completed_requests={start}/{len(jobs)} batch_size={current_batch_size} wait={wait_seconds:.0f}s", flush=True)

        def fetch(job, retry_wait):
            season, team, stat = job
            payload, attempts_used, source_status = get_payload(
                season, team, stat, args.timeout, 1, args.cache, retry_wait
            )
            expected = expected_by_pair[(season, team)]
            returned = {norm_name(row["Name"]) for row in payload["results"]}
            missing = expected - returned
            if missing:
                raise RuntimeError(f"{season}/{team}/{stat} missing {len(missing)} listed player name(s)")
            return {"job": job, "attempts": attempts_used, "source": source_status}

        # Retry failed requests immediately. Keep the batch size on the first
        # consecutive failed pass, decrease it on the second, and increase it
        # by one after a fully successful batch.
        pending = list(batch)
        retries_for_batch = 0
        while pending:
            with ThreadPoolExecutor(max_workers=min(args.workers, current_batch_size, len(pending))) as pool:
                future_to_job = {pool.submit(fetch, job, wait_seconds): job for job in pending}
                failed = []
                for future in as_completed(future_to_job):
                    job = future_to_job[future]
                    try:
                        outcomes[job] = future.result()
                    except Exception as exc:
                        failures_total += 1
                        failed.append(job)
                        print(f"REQUEST_FAILURE batch={batch_no+1} {job[0]}/{job[1]}/{job[2]}: {exc}", flush=True)
            if failed:
                retries_for_batch += 1
                if retries_for_batch >= args.attempts:
                    raise RuntimeError(f"Batch {batch_no+1} failed after {retries_for_batch} batch retry rounds")
                failure_streak += 1
                if failure_streak >= 2:
                    current_batch_size = max(1, current_batch_size - 1)
                    failure_streak = 0
                print(f"RETRY batch={batch_no+1} failed_requests={len(failed)} next_batch_size={current_batch_size} wait={wait_seconds:.0f}s", flush=True)
                checkpoint = {"next_job": start, "completed_batches": batch_no, "wait_seconds": wait_seconds,
                              "current_batch_size": current_batch_size, "failure_streak": failure_streak,
                              "total_jobs": len(jobs),
                              "team_season_pairs": len(pairs), "stats_per_pair": list(STATS)}
                write_progress(args.progress, checkpoint)
            pending = failed

        wait_seconds = max(0.0, wait_seconds - 5.0)
        current_batch_size = min(args.workers, current_batch_size + 1)
        failure_streak = 0
        batch_no += 1
        batch_in_invocation += 1
        start += len(batch)
        state = {"next_job": start, "completed_batches": batch_no, "wait_seconds": wait_seconds,
                 "current_batch_size": current_batch_size, "failure_streak": failure_streak,
                 "total_jobs": len(jobs), "team_season_pairs": len(pairs), "stats_per_pair": list(STATS)}
        write_progress(args.progress, state)
        print(f"BATCH_COMPLETE number={batch_no} requests={start}/{len(jobs)} wait={wait_seconds:.0f}s", flush=True)
        if batch_no % 10 == 0 or start == len(jobs):
            print(f"BATCH_MILESTONE completed={batch_no} requests={start}/{len(jobs)} pairs={len(pairs)} wait={wait_seconds:.0f}s", flush=True)
        elif batch_in_invocation == args.batch_limit:
            print(f"SEGMENT_COMPLETE completed={batch_no} requests={start}/{len(jobs)} wait={wait_seconds:.0f}s", flush=True)

    print(f"SEGMENT_DONE completed_batches={batch_no} segment_batches={batch_in_invocation} requests={start}/{len(jobs)} failures_retried={failures_total}", flush=True)

    if start < len(jobs):
        return

    # Materialize the raw API stats for all names seen for each pair.
    api = {}
    for season, team, stat in jobs:
        payload = json.loads(cache_path(args.cache, season, team, stat).read_text(encoding="utf-8"))
        for row in payload["results"]:
            api.setdefault((season, team, norm_name(row["Name"])), {})[stat] = row

    output = []
    for source_row, record in sorted(source.items(), key=lambda item: int(item[0])):
        season, team, name = record["name_key"]
        player_stats = api.get((season, team, name), {})
        missing_stats = set(STATS) - set(player_stats)
        if missing_stats:
            raise RuntimeError(f"source_row={source_row} missing stats {sorted(missing_stats)}")
        result = {k: v for k, v in record.items() if k != "name_key"}
        result["API_PLAYER_NAME"] = player_stats["FGA"]["Name"]
        values = {}
        for stat, prefix in (("OffPoss", "OFF_POSS"), ("DefPoss", "DEF_POSS"), ("FGA", "FGA"),
                             ("FTA", "FTA"), ("OpponentFGA", "OPP_FGA"), ("OpponentFTA", "OPP_FTA")):
            row = player_stats[stat]
            for status, label in (("On", "ON"), ("Off", "OFF")):
                value = float(row[status])
                values[(prefix, label)] = value
                result[f"{prefix}_{label}"] = value
        minutes_row = player_stats["FGA"]
        result["MIN_ON"] = float(minutes_row["MinutesOn"])
        result["MIN_OFF"] = float(minutes_row["MinutesOff"])
        for status in ("ON", "OFF"):
            own = lambda prefix: values[(prefix, status)]
            off_poss = own("OFF_POSS")
            def_poss = own("DEF_POSS")
            fga, fta = own("FGA"), own("FTA")
            opp_fga, opp_fta = own("OPP_FGA"), own("OPP_FTA")
            share = shares.get(source_row, {}).get(("3PAr", "On" if status == "ON" else "Off"))
            opp_share = shares.get(source_row, {}).get(("OPP_3PAr", "On" if status == "ON" else "Off"))
            if share is None or opp_share is None:
                raise RuntimeError(f"source_row={source_row} missing master 3PAr share for {status}")
            result[f"3PAr_{status}"] = share
            result[f"OPP_3PAr_{status}"] = opp_share
            result[f"FTR_{status}"] = fta / fga if fga else ""
            result[f"OPP_FTR_{status}"] = opp_fta / opp_fga if opp_fga else ""
            for label, count, denom in (("FGA", fga, off_poss), ("FTA", fta, off_poss),
                                         ("2PA", fga * (1 - share), off_poss), ("3PA", fga * share, off_poss)):
                result[f"{label}_PER_100_OFF_POSS_{status}"] = 100 * count / denom if denom else ""
            for label, count, denom in (("OPP_FGA", opp_fga, def_poss), ("OPP_FTA", opp_fta, def_poss),
                                         ("OPP_2PA", opp_fga * (1 - opp_share), def_poss),
                                         ("OPP_3PA", opp_fga * opp_share, def_poss)):
                result[f"{label}_PER_100_DEF_POSS_{status}"] = 100 * count / denom if denom else ""
        output.append(result)

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    fields = list(output[0])
    temp = args.output_csv.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="raise")
        writer.writeheader()
        writer.writerows(output)
    os.replace(temp, args.output_csv)
    print(f"OUTPUT_COMPLETE rows={len(output)} file={args.output_csv}", flush=True)


if __name__ == "__main__":
    main()
