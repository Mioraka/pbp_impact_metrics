#!/usr/bin/env python3
"""Append on/off 2PA and 3PA rates to the long player-season master table."""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

KEYS = ["Season", "SeasonType", "Player_id", "Team_id"]
METRIC_COLUMNS = {
    "2PA per 100 Possessions": {
        "On": "2PA_PER_100_OFF_POSS_ON",
        "Off": "2PA_PER_100_OFF_POSS_OFF",
    },
    "3PA per 100 Possessions": {
        "On": "3PA_PER_100_OFF_POSS_ON",
        "Off": "3PA_PER_100_OFF_POSS_OFF",
    },
    "2PA per 100 Possessions - Defense": {
        "On": "OPP_2PA_PER_100_DEF_POSS_ON",
        "Off": "OPP_2PA_PER_100_DEF_POSS_OFF",
    },
    "3PA per 100 Possessions - Defense": {
        "On": "OPP_3PA_PER_100_DEF_POSS_ON",
        "Off": "OPP_3PA_PER_100_DEF_POSS_OFF",
    },
}
NEW_METRICS = list(METRIC_COLUMNS)


def run(master_path: Path, volume_path: Path, output_path: Path) -> dict[str, int]:
    master = pd.read_csv(master_path, low_memory=False)
    volume = pd.read_csv(volume_path, low_memory=False)
    required_master = set(KEYS + ["source_row", "metrics", "Court Status"])
    required_volume = set(KEYS + [column for pair in METRIC_COLUMNS.values() for column in pair.values()])
    if not required_master.issubset(master.columns):
        raise ValueError(f"Master missing columns: {sorted(required_master - set(master.columns))}")
    if not required_volume.issubset(volume.columns):
        raise ValueError(f"Volume file missing columns: {sorted(required_volume - set(volume.columns))}")

    # Rebuild these rows each run so the merge is safe to rerun.
    master = master.loc[~master["metrics"].isin(NEW_METRICS)].copy()

    # Duplicate API rows are allowed only when all calculated values agree.
    volume_value_columns = [column for pair in METRIC_COLUMNS.values() for column in pair.values()]
    conflict_counts = volume.groupby(KEYS, dropna=False)[volume_value_columns].nunique(dropna=False)
    if (conflict_counts > 1).any().any():
        raise ValueError("Conflicting duplicate player/team/season rows in volume input")
    volume = volume.drop_duplicates(KEYS, keep="first")

    metadata = master.sort_values("source_row").drop_duplicates("source_row").copy()
    joined = metadata[KEYS + ["source_row"]].merge(
        volume[KEYS + volume_value_columns], on=KEYS, how="left", validate="many_to_one", indicator=True
    )
    unmatched = int(joined["_merge"].ne("both").sum())
    if unmatched:
        missing = joined.loc[joined["_merge"].ne("both"), KEYS].head(5).to_dict("records")
        raise ValueError(f"{unmatched} master source rows lack volume data; examples: {missing}")

    template_columns = master.columns.tolist()
    additions: list[dict[str, object]] = []
    for record in joined.to_dict("records"):
        base = metadata.loc[metadata["source_row"].eq(record["source_row"])].iloc[0].to_dict()
        for metric, columns in METRIC_COLUMNS.items():
            on_value = record[columns["On"]]
            off_value = record[columns["Off"]]
            for court_status, value in (("On", on_value), ("Off", off_value), ("On-Off", on_value - off_value)):
                row = {column: base.get(column, pd.NA) for column in template_columns}
                row["metrics"] = metric
                row["Court Status"] = court_status
                row["PlayerValue"] = value
                # These newly requested derived rates are absent from the league-wide source.
                row["LeagueAverage"] = pd.NA
                row["PlayerMinusLeague"] = pd.NA
                additions.append(row)

    result = pd.concat([master, pd.DataFrame(additions, columns=template_columns)], ignore_index=True)
    result.to_csv(output_path, index=False, na_rep="")
    return {
        "input_master_rows": len(master),
        "input_volume_rows": len(pd.read_csv(volume_path, low_memory=False)),
        "unique_volume_player_team_seasons": len(volume),
        "new_metric_rows": len(additions),
        "output_rows": len(result),
        "output_metrics": int(result["metrics"].nunique()),
        "duplicate_volume_rows_removed": int(pd.read_csv(volume_path, low_memory=False).duplicated(KEYS).sum()),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--master", default="data/player_league_master.csv")
    parser.add_argument("--volume", default="data/player_on_off_volume.csv")
    parser.add_argument("--output", default="data/player_league_master.csv")
    args = parser.parse_args()
    summary = run(Path(args.master), Path(args.volume), Path(args.output))
    for key, value in summary.items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
