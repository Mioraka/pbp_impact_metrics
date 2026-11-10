#!/usr/bin/env python3
"""Pull player on/off and league-season PBPStats, then join them.

The input player list defines the player-season-team rows. Each successful
player request must contain the complete 89-stat contract. League totals are
requested once per season and transformed through the same metric dictionary.
Outputs are installed atomically only after every request and join validates.
"""
from __future__ import annotations

import argparse
import ast
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed


API = "https://api.pbpstats.com"
PLAYER_ENDPOINT = API + "/get-on-off/nba/team"
LEAGUE_ENDPOINT = API + "/get-totals/nba"
SEASON_TYPE = "Regular Season"
PLAYER_FIELDS = ["source_row", "SEASON_YEAR", "SEASON", "Season_Player", "PLAYER_NAME",
                 "Player_id", "Team_id", "ALL_STAR", "ALL_NBA", "ALL_DEF", "MVP", "DPOY",
                 "Any Award", "metrics", "Court Status", "Value"]
MASTER_FIELDS = ["source_row", "SEASON_YEAR", "SEASON", "Season", "SeasonType",
                 "Season_Player", "PLAYER_NAME", "Player_id", "Team_id", "ALL_STAR",
                 "ALL_NBA", "ALL_DEF", "MVP", "DPOY", "Any Award", "metrics",
                 "Court Status", "PlayerValue", "LeagueAverage", "PlayerMinusLeague"]
RETRYABLE = {429, 500, 502, 503, 504}
STATUS_FIELDS = ("On", "Off", "On-Off")
FORMULA_OVERRIDES = {
    "FirstChancePtsPer100Poss": "100*FirstChancePoints/OffPoss",
    "PenaltyPtsPer100PossPenalty": "100*PenaltyPoints/PenaltyOffPoss",
    "PtsPer100Poss": "100*Points/OffPoss",
    "SecondChancePtsPer100PossSecondChance": "100*SecondChancePoints/SecondChanceOffPoss",
    "SecondChancePtsPer100Poss": "100*SecondChancePoints/OffPoss",
    "SecondChanceOffPossPer100Poss": "100*SecondChanceOffPoss/OffPoss",
    "SecondsPerPoss": "SecondsPerPossOff",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def season_code(value: str) -> str:
    """Normalize 2000-2001, 2000-01, and the PBP API's 2000-01 form."""
    text = str(value).strip()
    match = re.fullmatch(r"(\d{4})-(\d{2}|\d{4})", text)
    if not match:
        raise ValueError(f"Invalid season label: {value!r}")
    return f"{match.group(1)}-{match.group(2)[-2:]}"


def api_season(start_year: int) -> str:
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def atomic_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="raise")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"Missing CSV header: {path}")
        return list(reader.fieldnames), list(reader)


def read_metric_definitions(path: Path) -> list[dict[str, str]]:
    fields, rows = read_csv(path)
    required = {"metrics", "api_stat_id"}
    if not required.issubset(fields):
        raise ValueError(f"Metric dictionary requires columns: {sorted(required)}")
    labels = [row["metrics"].strip() for row in rows]
    if len(labels) != 89 or len(set(labels)) != 89 or any(not label for label in labels):
        raise ValueError(f"Expected exactly 89 unique metric definitions; found {len(set(labels))}")
    if any(not row["api_stat_id"].strip() for row in rows):
        raise ValueError("Empty API field/formula in metric dictionary")
    return rows


def validate_player_payload(payload: object, expected_metrics: set[str]) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        raise ValueError("PBP player response has no results list")
    rows = payload["results"]
    by_name = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("Stat"), str):
            raise ValueError("Malformed player metric row")
        name = row["Stat"].strip()
        if not name or name in by_name:
            raise ValueError(f"Empty or duplicate player metric: {name!r}")
        for status in STATUS_FIELDS:
            try:
                value = float(row[status])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Missing/non-numeric {status} for {name}") from exc
            if not math.isfinite(value):
                raise ValueError(f"Non-finite {status} for {name}")
        by_name[name] = row
    received = set(by_name)
    if received != expected_metrics:
        missing, extra = sorted(expected_metrics - received), sorted(received - expected_metrics)
        raise ValueError(f"Player metric contract mismatch: missing={missing[:5]}, extra={extra[:5]}, count={len(received)}")
    return [by_name[name] for name in sorted(expected_metrics)]


class ApiClient:
    def __init__(self, cache: Path, attempts: int = 7, timeout: int = 60):
        self.cache = cache
        self.cache.mkdir(parents=True, exist_ok=True)
        self.attempts = attempts
        self.timeout = timeout
        self.local = threading.local()
        self.log_lock = threading.Lock()
        self.logs: list[dict] = []

    def session(self):
        if not hasattr(self.local, "session"):
            import requests
            self.local.session = requests.Session()
            self.local.session.headers.update({"User-Agent": "pbp-impact-stats/2.0", "Accept": "application/json"})
        return self.local.session

    def get_json(self, kind: str, url: str, params: dict[str, str], validator) -> tuple[object, dict]:
        import requests
        prepared = requests.Request("GET", url, params=params).prepare()
        request_url = prepared.url
        cache_file = self.cache / (hashlib.sha256(request_url.encode()).hexdigest() + ".json.gz")
        start = time.monotonic()
        if cache_file.exists():
            try:
                with gzip.open(cache_file, "rt", encoding="utf-8") as handle:
                    saved = json.load(handle)
                validator(saved["payload"])
                info = {"kind": kind, "url": request_url, "status": "cache", "attempts": 0,
                        "seconds": round(time.monotonic() - start, 3), "retrieved_at_utc": saved["retrieved_at_utc"], "error": ""}
                with self.log_lock:
                    self.logs.append(info)
                return saved["payload"], info
            except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError):
                cache_file.unlink(missing_ok=True)

        last_error = ""
        for attempt in range(1, self.attempts + 1):
            wait = min(60, 2 ** (attempt - 1)) + random.uniform(0, 1)
            try:
                response = self.session().get(request_url, timeout=(15, self.timeout))
                if response.status_code not in RETRYABLE:
                    response.raise_for_status()
                else:
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            wait = max(wait, min(120, float(retry_after)))
                        except ValueError:
                            pass
                    raise requests.HTTPError(f"Retryable HTTP {response.status_code}", response=response)
                payload = response.json()
                validator(payload)
                retrieved = utcnow()
                temp = cache_file.with_suffix(".tmp")
                with gzip.open(temp, "wt", encoding="utf-8") as handle:
                    json.dump({"retrieved_at_utc": retrieved, "payload": payload}, handle, separators=(",", ":"))
                os.replace(temp, cache_file)
                info = {"kind": kind, "url": request_url, "status": response.status_code,
                        "attempts": attempt, "seconds": round(time.monotonic() - start, 3),
                        "retrieved_at_utc": retrieved, "error": ""}
                with self.log_lock:
                    self.logs.append(info)
                return payload, info
            except (requests.RequestException, ValueError, TypeError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                permanent = isinstance(exc, requests.HTTPError) and exc.response is not None and 400 <= exc.response.status_code < 500 and exc.response.status_code != 429
                if permanent or attempt == self.attempts:
                    break
                time.sleep(wait)
        info = {"kind": kind, "url": request_url, "status": "failed", "attempts": attempt,
                "seconds": round(time.monotonic() - start, 3), "retrieved_at_utc": "", "error": last_error}
        with self.log_lock:
            self.logs.append(info)
        raise RuntimeError(f"{kind} request failed after {info['attempts']} attempts: {last_error}")


def load_player_list(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    fields, rows = read_csv(path)
    required = {"SEASON", "PLAYER_NAME", "Player_id", "Team_id"}
    if not required.issubset(fields) or not rows:
        raise ValueError(f"Player list needs rows and columns {sorted(required)}")
    for row_number, row in enumerate(rows, 1):
        if not all(row.get(key, "").strip() for key in required):
            raise ValueError(f"Player list row {row_number} has missing season/player/team data")
        season_code(row["SEASON"])
        if not row["Player_id"].isdigit() or not row["Team_id"].isdigit():
            raise ValueError(f"Player list row {row_number} contains a nonnumeric player/team ID")
    return fields, rows


def extract_players(client: ApiClient, player_list: Path, metrics: list[dict[str, str]], output: Path,
                    workers: int, batch_size: int, batch_delay: float) -> list[dict[str, str]]:
    source_fields, source_rows = load_player_list(player_list)
    expected = {item["metrics"] for item in metrics}
    grouped: dict[tuple[str, str, str], list[tuple[int, dict[str, str]]]] = {}
    for source_row, row in enumerate(source_rows, 1):
        key = (season_code(row["SEASON"]), row["Team_id"].strip(), row["Player_id"].strip())
        grouped.setdefault(key, []).append((source_row, row))
    keys = sorted(grouped)

    def fetch(key):
        season, team_id, player_id = key
        params = {"Season": season, "SeasonType": SEASON_TYPE, "TeamId": team_id, "PlayerId": player_id}
        validate = lambda body: validate_player_payload(body, expected)
        body, _ = client.get_json("player", PLAYER_ENDPOINT, params, validate)
        return key, validate_player_payload(body, expected)

    fetched = {}
    for start in range(0, len(keys), batch_size):
        batch = keys[start:start + batch_size]
        with ThreadPoolExecutor(max_workers=min(workers, len(batch))) as pool:
            tasks = [pool.submit(fetch, key) for key in batch]
            for future in as_completed(tasks):
                key, rows = future.result()
                fetched[key] = rows
        if start + batch_size < len(keys) and batch_delay:
            time.sleep(batch_delay)

    output_rows = []
    for key, members in grouped.items():
        metrics_for_key = fetched[key]
        for source_row, player in members:
            for metric in metrics_for_key:
                for status in STATUS_FIELDS:
                    output_rows.append({"source_row": source_row, **player,
                                        "metrics": metric["Stat"], "Court Status": status,
                                        "Value": metric[status]})
    fields = ["source_row", *source_fields, "metrics", "Court Status", "Value"]
    output_rows.sort(key=lambda row: (int(row["source_row"]), row["metrics"], row["Court Status"]))
    expected_rows = len(source_rows) * len(metrics) * 3
    if len(output_rows) != expected_rows:
        raise ValueError(f"Player output row count {len(output_rows)} != expected {expected_rows}")
    atomic_csv(output, fields, output_rows)
    return output_rows


class FormulaEvaluator(ast.NodeVisitor):
    """Evaluate only arithmetic expressions over named fields in a totals row."""
    def __init__(self, fields: dict):
        self.fields = fields

    def visit_Expression(self, node): return self.visit(node.body)
    def visit_Name(self, node):
        if node.id not in self.fields:
            raise KeyError(f"Totals response lacks formula field {node.id}")
        return float(self.fields[node.id])
    def visit_Constant(self, node):
        if isinstance(node.value, (int, float)):
            return float(node.value)
        raise ValueError("Unsupported constant in metric formula")
    def visit_BinOp(self, node):
        left, right = self.visit(node.left), self.visit(node.right)
        if isinstance(node.op, ast.Add): return left + right
        if isinstance(node.op, ast.Sub): return left - right
        if isinstance(node.op, ast.Mult): return left * right
        if isinstance(node.op, ast.Div):
            if right == 0: raise ZeroDivisionError("Metric denominator is zero")
            return left / right
        raise ValueError("Unsupported operator in metric formula")
    def generic_visit(self, node):
        raise ValueError(f"Unsupported syntax in metric formula: {type(node).__name__}")


def extract_totals_row(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("League totals response is not an object")
    row = payload.get("single_row_table_data")
    if isinstance(row, list) and len(row) == 1:
        row = row[0]
    if not isinstance(row, dict) or not row:
        raise ValueError("League totals response must contain one single_row_table_data record")
    return row


def calculate_metric(definition: dict[str, str], raw: dict, prior_values: dict[str, float]) -> float:
    identifier = definition["api_stat_id"].strip()
    if identifier.startswith("league_symmetry:"):
        counterpart = identifier.split(":", 1)[1]
        if counterpart not in prior_values:
            raise KeyError(f"League symmetry source is not available yet: {counterpart}")
        return prior_values[counterpart]
    if identifier in raw:
        value = float(raw[identifier])
    else:
        expression = FORMULA_OVERRIDES.get(identifier, identifier)
        value = FormulaEvaluator(raw).visit(ast.parse(expression, mode="eval"))
    if not math.isfinite(value):
        raise ValueError(f"Non-finite league metric {definition['metrics']}")
    return value


def extract_league_averages(client: ApiClient, metrics: list[dict[str, str]], start_year: int,
                            end_year: int, output: Path) -> list[dict[str, str]]:
    rows = []
    for year in range(start_year, end_year + 1):
        season = api_season(year)
        params = {"Season": season, "SeasonType": SEASON_TYPE, "StartType": "All",
                  "GroupBy": "Season", "Type": "Team"}
        payload, info = client.get_json("league", LEAGUE_ENDPOINT, params, extract_totals_row)
        raw = extract_totals_row(payload)
        values = {}
        remaining = list(metrics)
        while remaining:
            progress = False
            for definition in remaining[:]:
                try:
                    value = calculate_metric(definition, raw, values)
                except KeyError:
                    continue
                values[definition["metrics"]] = value
                remaining.remove(definition)
                progress = True
            if not progress:
                raise ValueError(f"Could not resolve league metrics: {[r['metrics'] for r in remaining]}")
        for definition in metrics:
            rows.append({"Season": season, "SeasonType": SEASON_TYPE,
                         "metrics": definition["metrics"], "Value": values[definition["metrics"]],
                         "api_stat_id": definition["api_stat_id"], "retrieved_at_utc": info["retrieved_at_utc"]})
    if len(rows) != (end_year - start_year + 1) * 89:
        raise ValueError("League-season output failed complete metric coverage")
    atomic_csv(output, ["Season", "SeasonType", "metrics", "Value", "api_stat_id", "retrieved_at_utc"], rows)
    return rows


def build_master(player_list: Path, player_stats: Path, league_averages: Path, output: Path,
                 metrics: list[dict[str, str]]) -> list[dict[str, str]]:
    _, players = load_player_list(player_list)
    player_fields, stats = read_csv(player_stats)
    _, league = read_csv(league_averages)
    player_keys = {str(i) for i in range(1, len(players) + 1)}
    source_count = len(players)
    expected_names = {row["metrics"] for row in metrics}
    metric_by_season: dict[tuple[str, str], str] = {}
    for row in league:
        key = (season_code(row["Season"]), row["metrics"])
        if key in metric_by_season:
            raise ValueError(f"Duplicate league average key {key}")
        metric_by_season[key] = row["Value"]
    seasons = {season_code(p["SEASON"]) for p in players}
    missing_league = [(s, m) for s in seasons for m in expected_names if (s, m) not in metric_by_season]
    if missing_league:
        raise ValueError(f"Missing league averages for {len(missing_league)} season/metric pairs; first={missing_league[:3]}")
    source_metric_counts: dict[str, set[str]] = {key: set() for key in player_keys}
    source_status_counts: dict[tuple[str, str], int] = {}
    master = []
    for row in stats:
        source_row = str(row.get("source_row", ""))
        metric = row.get("metrics", "")
        status = row.get("Court Status", "")
        if source_row not in player_keys or metric not in expected_names or status not in STATUS_FIELDS:
            raise ValueError(f"Unexpected player output row: source_row={source_row}, metric={metric}, status={status}")
        source_metric_counts[source_row].add(metric)
        combo = (source_row, metric, status)
        source_status_counts[combo] = source_status_counts.get(combo, 0) + 1
        season = season_code(row["SEASON"])
        league_value = float(metric_by_season[(season, metric)])
        player_value = float(row["Value"])
        master.append({"source_row": source_row, "SEASON_YEAR": row.get("SEASON_YEAR", ""),
                       "SEASON": row["SEASON"], "Season": season, "SeasonType": SEASON_TYPE,
                       "Season_Player": row.get("Season_Player", ""), "PLAYER_NAME": row["PLAYER_NAME"],
                       "Player_id": row["Player_id"], "Team_id": row["Team_id"],
                       "ALL_STAR": row.get("ALL_STAR", ""), "ALL_NBA": row.get("ALL_NBA", ""),
                       "ALL_DEF": row.get("ALL_DEF", ""), "MVP": row.get("MVP", ""),
                       "DPOY": row.get("DPOY", ""), "Any Award": row.get("Any Award", ""),
                       "metrics": metric, "Court Status": status, "PlayerValue": player_value,
                       "LeagueAverage": league_value, "PlayerMinusLeague": player_value - league_value})
    expected_total = source_count * 89 * 3
    if len(master) != expected_total:
        raise ValueError(f"Master rows={len(master)}; expected {expected_total}")
    for source_row in player_keys:
        if source_metric_counts[source_row] != expected_names:
            raise ValueError(f"Source row {source_row} lacks one or more of the 89 metrics")
    if any(count != 1 for count in source_status_counts.values()):
        raise ValueError("Duplicate source-row/metric/court-status records")
    if len(source_status_counts) != expected_total:
        raise ValueError("Player output has missing metric/status combinations")
    master.sort(key=lambda row: (int(row["source_row"]), row["metrics"], row["Court Status"]))
    atomic_csv(output, MASTER_FIELDS, master)
    return master


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--player-list", type=Path, default=Path("data/player_list.csv"))
    parser.add_argument("--metric-definitions", type=Path, default=Path("data/metric_definitions.csv"))
    parser.add_argument("--player-output", type=Path, default=Path("data/player_advanced_metrics.csv"))
    parser.add_argument("--league-output", type=Path, default=Path("data/league_season_averages.csv"))
    parser.add_argument("--master-output", type=Path, default=Path("data/player_league_master.csv"))
    parser.add_argument("--cache", type=Path, default=Path(".cache/player-league-v2"))
    parser.add_argument("--start-year", type=int, default=2000)
    parser.add_argument("--end-year", type=int, default=2025)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=3)
    parser.add_argument("--batch-delay", type=float, default=2.0)
    parser.add_argument("--attempts", type=int, default=7)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--skip-pull", action="store_true", help="Build master from existing output files")
    args = parser.parse_args()
    if args.start_year > args.end_year or args.workers < 1 or args.batch_size < 1 or args.attempts < 1:
        parser.error("Invalid year, concurrency, or attempt setting")
    metrics = read_metric_definitions(args.metric_definitions)
    client = ApiClient(args.cache, attempts=args.attempts, timeout=args.timeout)
    started = utcnow()
    try:
        if not args.skip_pull:
            extract_players(client, args.player_list, metrics, args.player_output,
                            args.workers, args.batch_size, args.batch_delay)
            extract_league_averages(client, metrics, args.start_year, args.end_year, args.league_output)
        master = build_master(args.player_list, args.player_output, args.league_output,
                              args.master_output, metrics)
        manifest = {"complete": True, "started_at_utc": started, "finished_at_utc": utcnow(),
                    "season_type": SEASON_TYPE, "player_source_rows": len(load_player_list(args.player_list)[1]),
                    "player_metric_status_rows": len(read_csv(args.player_output)[1]),
                    "league_season_metric_rows": len(read_csv(args.league_output)[1]),
                    "master_rows": len(master), "metric_count": 89,
                    "player_row_key": ["source_row", "SEASON", "Player_id", "Team_id"],
                    "league_row_key": ["Season", "SeasonType", "metrics"],
                    "join_key": ["normalized Season", "SeasonType", "metrics"],
                    "request_count": len(client.logs), "failed_requests": 0}
        write_json(args.master_output.parent / "run_manifest.json", manifest)
        write_json(args.master_output.parent / "request_log.json", {"requests": client.logs})
        print(json.dumps(manifest, indent=2))
    except Exception as exc:
        write_json(args.master_output.parent / "failed_run.json", {
            "complete": False, "started_at_utc": started, "failed_at_utc": utcnow(),
            "error": f"{type(exc).__name__}: {exc}", "request_count": len(client.logs),
            "requests": client.logs})
        raise


if __name__ == "__main__":
    main()
