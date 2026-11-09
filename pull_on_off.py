#!/usr/bin/env python3
"""Download all NBA player-season-team on/off metrics from PBP Stats.

Python 3.10+; pip install -r requirements.txt
Run: python pull_on_off.py
Resume an interrupted run with the same command. Use --refresh for a new snapshot.
"""
import argparse
import concurrent.futures as futures
import csv
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import gzip
import hashlib
import json
import math
from pathlib import Path
import random
import re
import threading
import time

import requests

BASE = "https://api.pbpstats.com"
IDENTIFIERS = ["league", "season", "season_type", "player_id", "player_name",
               "team_id", "team_abbreviation", "games_played", "seconds_played",
               "metric_count", "retrieved_at_utc", "source_url"]


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_csv(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def validate_payload(kind, data):
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object")
    if kind in ("teams", "players"):
        rows = data.get("multi_row_table_data")
        if not isinstance(rows, list):
            raise ValueError("Missing multi_row_table_data")
        # Fail closed on a possible cap instead of silently publishing 500 rows.
        if len(rows) >= 500:
            raise ValueError("Partition reached 500 records; subdivide before publication")
        required = ("EntityId",) if kind == "teams" else ("EntityId", "Name")
        for row in rows:
            if not isinstance(row, dict) or any(key not in row for key in required):
                raise ValueError("Malformed discovery row")
    elif kind == "onoff":
        rows = data.get("results")
        if not isinstance(rows, list) or not rows:
            raise ValueError("Missing or empty on/off results")
        names = []
        for row in rows:
            if not isinstance(row, dict) or not all(k in row for k in ("Stat", "On", "Off", "On-Off")):
                raise ValueError("Malformed on/off metric")
            names.append(row["Stat"])
            for key in ("On", "Off", "On-Off"):
                if not math.isfinite(float(row[key])):
                    raise ValueError("Non-finite metric")
        if len(set(names)) != len(names):
            raise ValueError("Duplicate metric names")
        if "Pts per 100 Possessions" not in names or "Pts per 100 Possessions - Defense" not in names:
            raise ValueError("Core offense/defense metrics missing")
    else:
        raise ValueError(f"Unknown request kind: {kind}")
    return len(rows)


class Client:
    def __init__(self, cache, attempts=7, timeout=60, refresh=False):
        self.cache = cache
        cache.mkdir(parents=True, exist_ok=True)
        self.attempts, self.timeout, self.refresh = attempts, timeout, refresh
        self.local = threading.local()
        self.lock = threading.Lock()
        self.queries, self.failures = [], []

    def session(self):
        if not hasattr(self.local, "session"):
            self.local.session = requests.Session()
            self.local.session.headers.update({"User-Agent": "pbp-impact-metrics/1.0", "Accept": "application/json"})
        return self.local.session

    def get(self, kind, params):
        endpoint = "/get-on-off/nba/team" if kind == "onoff" else "/get-totals/nba"
        # Use an unambiguous percent-encoded space in season types.
        url = requests.Request("GET", BASE + endpoint, params=params).prepare().url.replace("+", "%20")
        path = self.cache / (hashlib.sha256(url.encode()).hexdigest() + ".json.gz")
        info = {"kind": kind, "url": url, "season": params["Season"],
                "season_type": params["SeasonType"], "team_id": params.get("TeamId", ""),
                "player_id": params.get("PlayerId", ""), "attempts": 0,
                "cache_hit": False, "status": "failed", "records": 0,
                "retrieved_at_utc": "", "error": ""}
        if path.exists() and not self.refresh:
            try:
                with gzip.open(path, "rt", encoding="utf-8") as handle:
                    saved = json.load(handle)
                count = validate_payload(kind, saved["data"])
                info.update(cache_hit=True, status="ok", records=count,
                            retrieved_at_utc=saved["retrieved_at_utc"])
                with self.lock:
                    self.queries.append(info)
                return saved["data"], info
            except (OSError, ValueError, KeyError, TypeError):
                pass  # Invalid checkpoints are fetched again.
        error = ""
        for attempt in range(1, self.attempts + 1):
            delay = min(60, 2 ** (attempt - 1)) + random.uniform(0, 1)
            try:
                response = self.session().get(url, timeout=(15, self.timeout))
                info["attempts"] = attempt
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            delay = max(delay, float(retry_after))
                        except ValueError:
                            try:
                                delay = max(delay, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                            except (ValueError, TypeError):
                                pass
                    raise requests.HTTPError(f"Retryable HTTP {response.status_code}")
                response.raise_for_status()
                data = response.json()
                count = validate_payload(kind, data)
                timestamp = utcnow()
                temporary = path.with_suffix(".tmp")
                with gzip.open(temporary, "wt", encoding="utf-8") as handle:
                    json.dump({"url": url, "retrieved_at_utc": timestamp, "data": data}, handle, separators=(",", ":"))
                temporary.replace(path)
                info.update(status="ok", records=count, retrieved_at_utc=timestamp)
                with self.lock:
                    self.queries.append(info)
                return data, info
            except (requests.RequestException, ValueError, TypeError) as exc:
                error = f"{type(exc).__name__}: {exc}"
                info["attempts"] = attempt
                # Permanent 4xx errors cannot be fixed by repeating the request.
                if isinstance(exc, requests.HTTPError) and exc.response is not None and 400 <= exc.response.status_code < 500 and exc.response.status_code != 429:
                    break
                if attempt < self.attempts:
                    time.sleep(delay)
        info["error"] = error
        with self.lock:
            self.queries.append(info)
            self.failures.append(info)
        raise RuntimeError(f"Exhausted retries: {url}: {error}")


def parallel(items, function, workers, label):
    output, errors = [], []
    started = time.monotonic()
    with futures.ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {pool.submit(function, item): item for item in items}
        for index, future in enumerate(futures.as_completed(pending), 1):
            try:
                output.append(future.result())
            except Exception as exc:
                errors.append(str(exc))
                print(f"ERROR {exc}", flush=True)
            if index % 100 == 0 or index == len(items):
                print(f"{label}: {index}/{len(items)}, failures={len(errors)}, elapsed={time.monotonic()-started:.1f}s", flush=True)
    return output, errors


def metric_slug(name):
    # Collision detection below prevents distinct API labels sharing columns.
    return re.sub(r"[^a-z0-9]+", "_", name.lower().replace("%", "_pct")).strip("_")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=2000)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--season-types", nargs="+", default=["Regular Season", "Playoffs", "PlayIn"])
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--attempts", type=int, default=7)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--cache", type=Path, default=Path(".cache"))
    parser.add_argument("--output", type=Path, default=Path("data"))
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    if args.start_year > args.end_year or args.workers < 1 or args.attempts < 1:
        parser.error("Invalid year range, workers or attempts")
    output = args.output
    output.mkdir(parents=True, exist_ok=True)
    client = Client(args.cache, args.attempts, args.timeout, args.refresh)
    started = utcnow()
    # Play-in began in 2019-20. No games have occurred in 2026-27 postseason.
    partitions = [{"Season": f"{year}-{(year+1)%100:02d}", "SeasonType": kind, "Type": "Team"}
                  for year in range(args.start_year, args.end_year + 1)
                  for kind in args.season_types if kind != "PlayIn" or year >= 2019]

    def discover_teams(params):
        data, _ = client.get("teams", params)
        return [(params, row) for row in data["multi_row_table_data"]]

    def save_failure_audit(errors):
        fields = ["kind", "url", "season", "season_type", "team_id", "player_id", "attempts", "cache_hit", "status", "records", "retrieved_at_utc", "error"]
        write_csv(output / "queries.csv", client.queries, fields)
        write_csv(output / "failures.csv", client.failures, fields)
        (output / "run_summary.json").write_text(json.dumps({
            "complete": False, "started_at_utc": started, "finished_at_utc": utcnow(),
            "start_year": args.start_year, "end_year": args.end_year,
            "season_types": args.season_types, "errors": errors,
        }, indent=2) + "\n")

    team_batches, discovery_errors = parallel(partitions, discover_teams, args.workers, "Season/team discovery")
    if discovery_errors:
        save_failure_audit(discovery_errors)
        raise SystemExit("Team discovery incomplete; resume to retry missing partitions before downloading players.")
    teams = [item for batch in team_batches for item in batch]

    def discover_players(item):
        params, team = item
        team_id = str(team["EntityId"])
        query = dict(params, Type="Player", TeamId=team_id)
        data, _ = client.get("players", query)
        players = []
        for row in data["multi_row_table_data"]:
            if str(row.get("TeamId")) != team_id:
                raise ValueError(f"Team filter mismatch: {query}")
            # Include everyone with playing time, including sub-minute appearances.
            if float(row.get("SecondsPlayed", 0)) <= 0:
                continue
            players.append({"league": "nba", "season": params["Season"], "season_type": params["SeasonType"],
                            "team_id": team_id, "team_abbreviation": row.get("TeamAbbreviation", team.get("Name", "")),
                            "player_id": str(row["EntityId"]), "player_name": row["Name"],
                            "games_played": row.get("GamesPlayed", ""), "seconds_played": row["SecondsPlayed"]})
        return players

    player_batches, errors = parallel(teams, discover_players, args.workers, "Team/player discovery")
    discovery_errors.extend(errors)
    if discovery_errors:
        save_failure_audit(discovery_errors)
        raise SystemExit("Player discovery incomplete; resume to retry missing teams before downloading on/off data.")
    players = [item for batch in player_batches for item in batch]
    players.sort(key=lambda row: (row["season"], row["season_type"], row["team_id"], row["player_id"]))
    keys = [(p["season"], p["season_type"], p["team_id"], p["player_id"]) for p in players]
    if len(set(keys)) != len(keys):
        raise RuntimeError("Duplicate player-season-team-season_type discovery keys")
    write_csv(output / "player_inventory.csv", players, list(players[0]) if players else ["season", "season_type", "team_id", "player_id"])

    def fetch_player(player):
        params = {"Season": player["season"], "SeasonType": player["season_type"],
                  "TeamId": player["team_id"], "PlayerId": player["player_id"]}
        data, info = client.get("onoff", params)
        row = dict(player, metric_count=len(data["results"]), retrieved_at_utc=info["retrieved_at_utc"], source_url=info["url"])
        return row, data["results"]

    print(f"Discovered {len(players)} player-season-team-season_type rows", flush=True)
    fetched, onoff_errors = parallel(players, fetch_player, args.workers, "On/off metrics")
    labels = sorted({metric["Stat"] for _, metrics in fetched for metric in metrics})
    slugs = [metric_slug(name) for name in labels]
    if len(set(slugs)) != len(slugs):
        raise RuntimeError("Metric column name collision")
    columns = IDENTIFIERS + [metric_slug(label) + "_" + suffix for label in labels for suffix in ("on", "off", "diff")]
    rows = []
    for row, metrics in fetched:
        for metric in metrics:
            for source, suffix in (("On", "on"), ("Off", "off"), ("On-Off", "diff")):
                row[metric_slug(metric["Stat"]) + "_" + suffix] = metric[source]
        rows.append(row)
    rows.sort(key=lambda row: (row["season"], row["season_type"], row["player_id"], row["team_id"]))
    query_fields = ["kind", "url", "season", "season_type", "team_id", "player_id", "attempts", "cache_hit", "status", "records", "retrieved_at_utc", "error"]
    queries = sorted(client.queries, key=lambda q: (q["season"], q["season_type"], q["kind"], q["team_id"], q["player_id"]))
    write_csv(output / "queries.csv", queries, query_fields)
    write_csv(output / "failures.csv", client.failures, query_fields)
    write_csv(output / "metric_dictionary.csv", [{"api_stat": label, "on_column": metric_slug(label)+"_on", "off_column": metric_slug(label)+"_off", "diff_column": metric_slug(label)+"_diff"} for label in labels], ["api_stat", "on_column", "off_column", "diff_column"])
    coverage = []
    for params in partitions:
        season, kind = params["Season"], params["SeasonType"]
        expected = [p for p in players if p["season"] == season and p["season_type"] == kind]
        actual = [r for r in rows if r["season"] == season and r["season_type"] == kind]
        coverage.append({"season": season, "season_type": kind, "teams": sum(p["Season"] == season and p["SeasonType"] == kind for p, _ in teams),
                         "unique_players": len({p["player_id"] for p in expected}), "expected_rows": len(expected), "downloaded_rows": len(actual),
                         "missing_rows": len(expected)-len(actual), "metric_count_min": min((r["metric_count"] for r in actual), default=""),
                         "metric_count_max": max((r["metric_count"] for r in actual), default="")})
    write_csv(output / "coverage.csv", coverage, list(coverage[0]))
    complete = not discovery_errors and not onoff_errors and len(rows) == len(players) and len(rows) > 0
    summary = {"started_at_utc": started, "finished_at_utc": utcnow(), "complete": complete,
               "start_year": args.start_year, "end_year": args.end_year, "season_types": args.season_types,
               "team_partitions": len(teams), "expected_rows": len(players), "downloaded_rows": len(rows),
               "unique_metric_labels": len(labels), "csv_columns": len(columns), "queries": len(queries),
               "failed_requests": len(client.failures), "errors": discovery_errors + onoff_errors,
               "row_key": ["league", "season", "season_type", "player_id", "team_id"]}
    (output / "run_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    if not complete:
        # Never replace a previously complete dataset with an incomplete pull.
        write_csv(output / "player_season_on_off.partial.csv", rows, columns)
        raise SystemExit("Incomplete extraction. See data/failures.csv and run_summary.json; resume with the same command.")
    write_csv(output / "player_season_on_off.csv", rows, columns)
    for season in sorted({row["season"] for row in rows}):
        write_csv(output / "by_season" / f"{season}.csv", [row for row in rows if row["season"] == season], columns)
    (output / "player_season_on_off.partial.csv").unlink(missing_ok=True)
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    main()
