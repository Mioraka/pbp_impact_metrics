#!/usr/bin/env python3
"""Validate and combine independently downloaded season partitions."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

from pull_on_off import IDENTIFIERS, utcnow, write_csv


def read_csv(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def combine(source, output, start_year, end_year):
    summaries = []
    rows, inventories, queries, coverage = [], [], [], []
    dictionaries = {}
    for year in range(start_year, end_year + 1):
        folder = source / str(year)
        summary = json.loads((folder / "run_summary.json").read_text())
        if not summary.get("complete") or summary["start_year"] != year or summary["end_year"] != year:
            raise ValueError(f"Missing or incomplete season {year}")
        expected_types = {"Regular Season", "Playoffs", "PlayIn"}
        if set(summary["season_types"]) != expected_types:
            raise ValueError(f"Season type mismatch in {year}")
        failures = read_csv(folder / "failures.csv")
        if failures:
            raise ValueError(f"Unresolved failures in {year}")
        part = read_csv(folder / "player_season_on_off.csv")
        inventory = read_csv(folder / "player_inventory.csv")
        if len(part) != summary["expected_rows"] or len(part) != summary["downloaded_rows"] or len(part) != len(inventory):
            raise ValueError(f"Count mismatch in {year}")
        if any(row["season"] != f"{year}-{(year+1)%100:02d}" for row in part):
            raise ValueError(f"Season leakage in {year}")
        keys = lambda table: [(r["league"], r["season"], r["season_type"], r["player_id"], r["team_id"]) for r in table]
        if set(keys(part)) != set(keys(inventory)) or len(set(keys(part))) != len(part):
            raise ValueError(f"Key mismatch or duplicates in {year}")
        for row in read_csv(folder / "metric_dictionary.csv"):
            if row["api_stat"] in dictionaries and dictionaries[row["api_stat"]] != row:
                raise ValueError("Metric dictionary mismatch")
            dictionaries[row["api_stat"]] = row
        season_coverage = read_csv(folder / "coverage.csv")
        if any(int(r["missing_rows"]) != 0 for r in season_coverage):
            raise ValueError(f"Missing coverage in {year}")
        rows.extend(part)
        inventories.extend(inventory)
        queries.extend(read_csv(folder / "queries.csv"))
        coverage.extend(season_coverage)
        summaries.append(summary)

    metrics = [dictionaries[k] for k in sorted(dictionaries)]
    columns = IDENTIFIERS + [m[k] for m in metrics for k in ("on_column", "off_column", "diff_column")]
    if len(columns) != len(set(columns)):
        raise ValueError("Column collision")
    rows.sort(key=lambda r: (r["season"], r["season_type"], r["player_id"], r["team_id"]))
    # Perform all validations above before touching published files.
    write_csv(output / "player_season_on_off.csv", rows, columns)
    for season in sorted({r["season"] for r in rows}):
        write_csv(output / "by_season" / f"{season}.csv", [r for r in rows if r["season"] == season], columns)
    for name, table in (("player_inventory", inventories), ("queries", queries), ("coverage", coverage)):
        write_csv(output / f"{name}.csv", table, list(table[0]))
    write_csv(output / "failures.csv", [], list(queries[0]))
    write_csv(output / "metric_dictionary.csv", metrics, list(metrics[0]))
    summary = {
        "complete": True, "started_at_utc": min(s["started_at_utc"] for s in summaries),
        "finished_at_utc": utcnow(), "start_year": start_year, "end_year": end_year,
        "season_types": ["Regular Season", "Playoffs", "PlayIn"],
        "expected_rows": len(inventories), "downloaded_rows": len(rows),
        "unique_player_seasons": len({(r["season"], r["season_type"], r["player_id"]) for r in rows}),
        "unique_metric_labels": len(metrics), "csv_columns": len(columns),
        "queries": len(queries), "failed_requests": 0,
        "row_key": ["league", "season", "season_type", "player_id", "team_id"],
        "season_runs": summaries,
    }
    (output / "run_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    checksums = []
    for path in sorted(output.rglob("*.csv")):
        checksums.append({"file": str(path.relative_to(output)), "bytes": path.stat().st_size,
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    write_csv(output / "checksums.csv", checksums, ["file", "bytes", "sha256"])
    print(json.dumps({k: v for k, v in summary.items() if k != "season_runs"}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("season_results"))
    parser.add_argument("--output", type=Path, default=Path("data"))
    parser.add_argument("--start-year", type=int, default=2000)
    parser.add_argument("--end-year", type=int, default=2026)
    args = parser.parse_args()
    combine(args.source, args.output, args.start_year, args.end_year)
