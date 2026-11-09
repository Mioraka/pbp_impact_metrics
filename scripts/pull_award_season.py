#!/usr/bin/env python3
"""Validate and pull one NBA All-NBA / All-Defensive season through NBA Stats."""

import argparse
import csv
import json
import random
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = "https://stats.nba.com/stats"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.nba.com",
    "Referer": "https://www.nba.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
}
OUTPUT_FIELDS = [
    "player", "season", "team", "player_id", "team_id", "award",
    "selection_team", "api_description", "source_url",
]


def normalize_name(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]", "", value.lower())


def result_set(payload, name):
    sets = payload.get("resultSets", payload.get("resultSet", []))
    if isinstance(sets, dict):
        sets = [sets]
    for result in sets:
        if result.get("name") == name:
            headers = result.get("headers", [])
            rows = result.get("rowSet", [])
            return [dict(zip(headers, row)) for row in rows]
    raise ValueError(f"NBA response is missing result set {name!r}")


class NBAStatsClient:
    def __init__(self, timeout=30, attempts=4, pause=1.0):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.timeout = timeout
        self.attempts = attempts
        self.pause = pause
        self.audit = []

    def get_json(self, endpoint, params):
        url = f"{BASE}/{endpoint}"
        last_error = None
        for attempt in range(1, self.attempts + 1):
            started = time.monotonic()
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                elapsed = round(time.monotonic() - started, 3)
                self.audit.append({
                    "endpoint": endpoint,
                    "url": response.url,
                    "status_code": response.status_code,
                    "attempt": attempt,
                    "elapsed_seconds": elapsed,
                    "response_bytes": len(response.content),
                    "error": "",
                })
                if response.status_code == 429 or response.status_code >= 500:
                    wait = min(30, 2 ** (attempt - 1)) + random.uniform(0, 0.5)
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            wait = max(wait, float(retry_after))
                        except ValueError:
                            pass
                    last_error = f"HTTP {response.status_code}: {response.text[:300]}"
                    if attempt < self.attempts:
                        time.sleep(wait)
                        continue
                    break
                response.raise_for_status()
                payload = response.json()
                time.sleep(self.pause)
                return payload
            except (requests.RequestException, ValueError) as exc:
                elapsed = round(time.monotonic() - started, 3)
                last_error = f"{type(exc).__name__}: {exc}"
                if not self.audit or self.audit[-1].get("url") != url:
                    self.audit.append({
                        "endpoint": endpoint,
                        "url": url,
                        "status_code": "",
                        "attempt": attempt,
                        "elapsed_seconds": elapsed,
                        "response_bytes": 0,
                        "error": last_error,
                    })
                if attempt < self.attempts:
                    time.sleep(min(30, 2 ** (attempt - 1)) + random.uniform(0, 0.5))
        raise RuntimeError(f"{endpoint} failed after {self.attempts} attempts: {last_error}")


def read_candidates(path, season):
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    required = {"player", "season", "team", "team_id", "award", "selection_team", "source_url"}
    if not rows or not required.issubset(rows[0]):
        raise ValueError(f"Candidate file must contain columns: {sorted(required)}")
    if any(row["season"] != season for row in rows):
        raise ValueError("Candidate file includes a season other than the requested season")
    return rows


def player_id_index(client, season):
    payload = client.get_json("commonallplayers", {
        "LeagueID": "00",
        "Season": season,
        "IsOnlyCurrentSeason": "0",
    })
    rows = result_set(payload, "CommonAllPlayers")
    index = {}
    for row in rows:
        name = row.get("DISPLAY_FIRST_LAST") or row.get("DISPLAY_LAST_COMMA_FIRST")
        player_id = row.get("PERSON_ID")
        if name and player_id:
            index[normalize_name(name)] = str(player_id)
    return index, len(rows)


def is_requested_award(description, award):
    value = normalize_name(description)
    if award == "All-NBA":
        return "allnba" in value
    if award == "All-Defensive":
        return "alldefensive" in value or "alldefense" in value
    return False


def pull_season(season, candidate_path, output_path, audit_path):
    client = NBAStatsClient()
    candidates = read_candidates(candidate_path, season)
    ids, registry_count = player_id_index(client, season)
    not_found = [row["player"] for row in candidates if normalize_name(row["player"]) not in ids]
    if not_found:
        raise ValueError(f"Player IDs not found by CommonAllPlayers: {not_found}")

    by_id = {}
    for row in candidates:
        by_id.setdefault(ids[normalize_name(row["player"])], []).append(row)

    output = []
    validation = {"season": season, "expected_rows": len(candidates), "matched_rows": 0, "missing": []}
    for player_id, player_candidates in by_id.items():
        payload = client.get_json("playerawards", {"PlayerID": player_id})
        awards = result_set(payload, "PlayerAwards")
        for candidate in player_candidates:
            match = next((
                award_row for award_row in awards
                if str(award_row.get("SEASON", "")) == season
                and is_requested_award(award_row.get("DESCRIPTION", ""), candidate["award"])
            ), None)
            if match is None:
                validation["missing"].append({
                    "player": candidate["player"],
                    "award": candidate["award"],
                    "season": season,
                    "player_id": player_id,
                    "candidate_team": candidate["team"],
                    "api_awards_for_season": [
                        row.get("DESCRIPTION") for row in awards
                        if str(row.get("SEASON", "")) == season
                    ],
                })
                continue
            output.append({
                "player": candidate["player"],
                "season": season,
                "team": candidate["team"],
                "player_id": player_id,
                "team_id": candidate["team_id"],
                "award": candidate["award"],
                "selection_team": candidate["selection_team"],
                "api_description": match.get("DESCRIPTION", ""),
                "source_url": candidate["source_url"],
            })
            validation["matched_rows"] += 1

    validation["unique_players"] = len(by_id)
    validation["common_all_players_records"] = registry_count
    validation["api_requests"] = len(client.audit)
    validation["requests"] = client.audit
    validation["completed_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(json.dumps(validation, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if validation["missing"]:
        raise ValueError(f"NBA PlayerAwards missed {len(validation['missing'])} of {len(candidates)} expected selections; see {audit_path}")
    output.sort(key=lambda row: (row["player"].casefold(), row["award"], row["selection_team"]))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        writer.writerows(output)
    print(json.dumps({key: validation[key] for key in ("season", "expected_rows", "matched_rows", "unique_players", "common_all_players_records", "api_requests")}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", default="2025-26")
    parser.add_argument("--candidates", type=Path, default=Path("data/award_candidates_2025-26.csv"))
    parser.add_argument("--output", type=Path, default=Path("data/player_award_selections_2025-26.csv"))
    parser.add_argument("--audit", type=Path, default=Path("validation/awards_2025-26_audit.json"))
    args = parser.parse_args()
    try:
        pull_season(args.season, args.candidates, args.output, args.audit)
    except Exception as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
