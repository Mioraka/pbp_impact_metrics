#!/usr/bin/env python3
"""Incrementally fetch missing player-season rows and current league averages."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from build_advanced_master import (
    ApiClient,
    atomic_csv,
    build_master,
    extract_league_averages,
    extract_players,
    load_player_list,
    read_csv,
    read_metric_definitions,
    season_code,
    utcnow,
    write_json,
)


def write_subset(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--player-list", type=Path, default=Path("data/player_list.csv"))
    parser.add_argument("--metric-definitions", type=Path, default=Path("data/metric_definitions.csv"))
    parser.add_argument("--player-output", type=Path, default=Path("data/player_advanced_metrics.csv"))
    parser.add_argument("--league-output", type=Path, default=Path("data/league_season_averages.csv"))
    parser.add_argument("--master-output", type=Path, default=Path("data/player_league_master.csv"))
    parser.add_argument("--cache", type=Path, default=Path(".cache/player-league-incremental"))
    parser.add_argument("--start-year", type=int, default=2000)
    parser.add_argument("--end-year", type=int, default=2026)
    parser.add_argument("--workers", type=int, default=9)
    parser.add_argument("--batch-size", type=int, default=9)
    parser.add_argument("--batch-delay", type=float, default=1.0)
    parser.add_argument("--attempts", type=int, default=7)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--refresh-season", action="append", default=[],
                        help="Refresh player rows for a season such as 2026-27.")
    parser.add_argument("--refresh-league-season", action="append", default=[],
                        help="Refresh the league baseline for a season such as 2026-27.")
    args = parser.parse_args()
    if args.start_year > args.end_year or args.workers < 1 or args.batch_size < 1 or args.attempts < 1:
        parser.error("Invalid year, concurrency, or attempt setting")

    metric_defs = read_metric_definitions(args.metric_definitions)
    source_fields, source_players = load_player_list(args.player_list)
    player_fields, existing_players = read_csv(args.player_output)
    league_fields, existing_league = read_csv(args.league_output)
    start = utcnow()
    client = ApiClient(args.cache, attempts=args.attempts, timeout=args.timeout)

    try:
        existing_source_rows = {int(row["source_row"]) for row in existing_players}
        refresh_seasons = {season_code(s) for s in args.refresh_season}
        refresh_global_rows = {
            index for index, row in enumerate(source_players, start=1)
            if season_code(row["SEASON"]) in refresh_seasons
        }
        requested_global_rows = [
            index for index, row in enumerate(source_players, start=1)
            if index not in existing_source_rows or index in refresh_global_rows
        ]

        existing_years = {
            int(season_code(row["Season"]).split("-")[0]) for row in existing_league
        }
        refresh_league_years = {
            int(season_code(s).split("-")[0]) for s in args.refresh_league_season
        }
        desired_years = set(range(args.start_year, args.end_year + 1))
        missing_years = sorted((desired_years - existing_years) | refresh_league_years)
        if any(year < args.start_year or year > args.end_year for year in missing_years):
            raise ValueError(f"League refresh season outside configured range: {missing_years}")

        with tempfile.TemporaryDirectory(prefix="pbp-incremental-") as temp_dir:
            temp = Path(temp_dir)
            fetched_player_rows = []
            if requested_global_rows:
                subset = [source_players[index - 1] for index in requested_global_rows]
                subset_path = temp / "requested_players.csv"
                subset_output = temp / "fetched_players.csv"
                write_subset(subset_path, source_fields, subset)
                fetched_player_rows = extract_players(
                    client, subset_path, metric_defs, subset_output,
                    args.workers, args.batch_size, args.batch_delay
                )
                for row in fetched_player_rows:
                    local_index = int(row["source_row"])
                    row["source_row"] = str(requested_global_rows[local_index - 1])

            updated_source_rows = set(requested_global_rows)
            combined_players = [
                row for row in existing_players
                if int(row["source_row"]) not in updated_source_rows
            ] + fetched_player_rows
            combined_players.sort(
                key=lambda row: (int(row["source_row"]), row["metrics"], row["Court Status"])
            )

            fetched_league_rows = []
            for year in missing_years:
                part = temp / f"league_{year}.csv"
                fetched_league_rows.extend(
                    extract_league_averages(client, metric_defs, year, year, part)
                )
            refreshed_codes = {f"{year}-{(year + 1) % 100:02d}" for year in missing_years}
            combined_league = [
                row for row in existing_league
                if season_code(row["Season"]) not in refreshed_codes
            ] + fetched_league_rows
            combined_league.sort(key=lambda row: (season_code(row["Season"]), row["metrics"]))

            staged_players = temp / "player_advanced_metrics.csv"
            staged_league = temp / "league_season_averages.csv"
            staged_master = temp / "player_league_master.csv"
            atomic_csv(staged_players, player_fields, combined_players)
            atomic_csv(staged_league, league_fields, combined_league)
            master_rows = build_master(
                args.player_list, staged_players, staged_league, staged_master, metric_defs
            )

            args.player_output.parent.mkdir(parents=True, exist_ok=True)
            args.league_output.parent.mkdir(parents=True, exist_ok=True)
            args.master_output.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged_players, args.player_output)
            os.replace(staged_league, args.league_output)
            os.replace(staged_master, args.master_output)

        manifest = {
            "complete": True,
            "started_at_utc": start,
            "finished_at_utc": utcnow(),
            "season_type": "Regular Season",
            "player_source_rows": len(source_players),
            "player_metric_status_rows": len(combined_players),
            "league_season_metric_rows": len(combined_league),
            "master_rows": len(master_rows),
            "metric_count": len(metric_defs),
            "player_row_key": ["source_row", "SEASON", "Player_id", "Team_id"],
            "league_row_key": ["Season", "SeasonType", "metrics"],
            "join_key": ["normalized Season", "SeasonType", "metrics"],
            "request_count": len(client.logs),
            "failed_requests": 0,
            "incremental_player_source_rows": len(requested_global_rows),
            "refreshed_or_added_league_seasons": [
                f"{year}-{(year + 1) % 100:02d}" for year in missing_years
            ],
        }
        write_json(args.master_output.parent / "run_manifest.json", manifest)
        write_json(args.master_output.parent / "request_log.json", {"requests": client.logs})
        print(json.dumps(manifest, indent=2))
    except Exception as exc:
        write_json(args.master_output.parent / "failed_run.json", {
            "complete": False,
            "started_at_utc": start,
            "failed_at_utc": utcnow(),
            "error": f"{type(exc).__name__}: {exc}",
            "request_count": len(client.logs),
            "requests": client.logs,
        })
        raise


if __name__ == "__main__":
    main()
