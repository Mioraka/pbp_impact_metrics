#!/usr/bin/env python3
"""Join on/off attempt-rate volume into the long player-season master table."""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

KEYS = ["Season", "SeasonType", "Player_id", "Team_id"]
VOLUME_METRICS = {
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
    "FTA per 100 Possessions - Defense": {
        "On": "OPP_FTA_PER_100_DEF_POSS_ON",
        "Off": "OPP_FTA_PER_100_DEF_POSS_OFF",
    },
}
ZONE_METRICS = {
    "At Rim FGA per 100 Possessions": (
        "FGA_PER_100_OFF_POSS",
        "At Rim Shot Frequency",
    ),
    "Short Mid Range FGA per 100 Possessions": (
        "FGA_PER_100_OFF_POSS",
        "Short Mid Range Shot Frequency",
    ),
    "Long Mid Range FGA per 100 Possessions": (
        "FGA_PER_100_OFF_POSS",
        "Long Mid Range Shot Frequency",
    ),
    "At Rim FGA per 100 Possessions - Defense": (
        "OPP_FGA_PER_100_DEF_POSS",
        "At Rim Shot Frequency - Defense",
    ),
    "Short Mid Range FGA per 100 Possessions - Defense": (
        "OPP_FGA_PER_100_DEF_POSS",
        "Short Mid Range Shot Frequency - Defense",
    ),
    "Long Mid Range FGA per 100 Possessions - Defense": (
        "OPP_FGA_PER_100_DEF_POSS",
        "Long Mid Range Shot Frequency - Defense",
    ),
}
FREQUENCY_METRICS = sorted({frequency for _, frequency in ZONE_METRICS.values()})
REBUILT_METRICS = list(VOLUME_METRICS) + list(ZONE_METRICS)


def run(master_path: Path, volume_path: Path, output_path: Path) -> dict[str, int]:
    master = pd.read_csv(master_path, low_memory=False)
    volume_raw = pd.read_csv(volume_path, low_memory=False)
    required_master = set(KEYS + ["source_row", "metrics", "Court Status", "PlayerValue", "LeagueAverage", "PlayerMinusLeague"])
    volume_value_columns = [column for pair in VOLUME_METRICS.values() for column in pair.values()]
    volume_value_columns += [
        f"{prefix}_{side}"
        for prefix in ("FTA_PER_100_OFF_POSS", "FGA_PER_100_OFF_POSS", "OPP_FGA_PER_100_DEF_POSS")
        for side in ("ON", "OFF")
    ]
    required_volume = set(KEYS + volume_value_columns)
    if not required_master.issubset(master.columns):
        raise ValueError(f"Master missing columns: {sorted(required_master - set(master.columns))}")
    if not required_volume.issubset(volume_raw.columns):
        raise ValueError(f"Volume file missing columns: {sorted(required_volume - set(volume_raw.columns))}")

    # These rows are regenerated, making the merge safe to rerun.
    master = master.loc[~master["metrics"].isin(REBUILT_METRICS)].copy()

    # Exact duplicate API rows are benign; conflicting duplicates stop the join.
    duplicate_value_columns = sorted(set(volume_value_columns))
    duplicate_counts = volume_raw.groupby(KEYS, dropna=False)[duplicate_value_columns].nunique(dropna=False)
    if (duplicate_counts > 1).any().any():
        raise ValueError("Conflicting duplicate player/team/season rows in volume input")
    volume = volume_raw.drop_duplicates(KEYS, keep="first")

    metadata = master.sort_values("source_row").drop_duplicates("source_row").copy()
    joined = metadata[KEYS + ["source_row"]].merge(
        volume[KEYS + duplicate_value_columns], on=KEYS, how="left", validate="many_to_one", indicator=True
    )
    unmatched = int(joined["_merge"].ne("both").sum())
    if unmatched:
        examples = joined.loc[joined["_merge"].ne("both"), KEYS].head(5).to_dict("records")
        raise ValueError(f"{unmatched} master source rows lack volume data; examples: {examples}")

    # Refresh the existing offensive FTA rate from the volume pull. It agrees
    # with the existing metric through 2025-26; this also brings 2026-27 current.
    fta_index = master.loc[master["metrics"].eq("FTA per 100 Possessions")].set_index(
        ["source_row", "Court Status"]
    )
    volume_by_source = joined.set_index("source_row")
    for status, side in (("On", "ON"), ("Off", "OFF")):
        source_values = volume_by_source[f"FTA_PER_100_OFF_POSS_{side}"]
        index = pd.MultiIndex.from_arrays([source_values.index, [status] * len(source_values)])
        if not index.isin(fta_index.index).all():
            raise ValueError(f"Missing existing FTA per 100 rows for {status}")
        mask = master["metrics"].eq("FTA per 100 Possessions") & master["Court Status"].eq(status)
        master.loc[mask, "PlayerValue"] = master.loc[mask, "source_row"].map(source_values)
    on = volume_by_source["FTA_PER_100_OFF_POSS_ON"]
    off = volume_by_source["FTA_PER_100_OFF_POSS_OFF"]
    swing = on - off
    swing_values = pd.Series(swing.to_numpy(), index=volume_by_source.index)
    mask = master["metrics"].eq("FTA per 100 Possessions") & master["Court Status"].eq("On-Off")
    master.loc[mask, "PlayerValue"] = master.loc[mask, "source_row"].map(swing_values)
    fta_mask = master["metrics"].eq("FTA per 100 Possessions")
    master.loc[fta_mask, "PlayerMinusLeague"] = (
        master.loc[fta_mask, "PlayerValue"] - master.loc[fta_mask, "LeagueAverage"]
    )

    # Zone frequencies are available in the master; multiply them by team or
    # opponent FGA per 100 possessions from the volume pull.
    frequency_rows = master[
        master["metrics"].isin(FREQUENCY_METRICS)
        & master["Court Status"].isin(["On", "Off"])
    ]
    frequencies = frequency_rows.set_index(["source_row", "metrics", "Court Status"])["PlayerValue"]
    expected_frequency_rows = len(metadata) * len(FREQUENCY_METRICS) * 2
    if len(frequency_rows) != expected_frequency_rows:
        raise ValueError("Missing or duplicate on/off shot-zone frequency values in master")

    template_columns = master.columns.tolist()
    additions: list[dict[str, object]] = []
    for record in joined.to_dict("records"):
        source_row = record["source_row"]
        base = metadata.loc[metadata["source_row"].eq(source_row)].iloc[0].to_dict()
        values_by_metric: dict[str, tuple[float, float]] = {}
        for metric, columns in VOLUME_METRICS.items():
            values_by_metric[metric] = (record[columns["On"]], record[columns["Off"]])
        for metric, (attempt_prefix, frequency_metric) in ZONE_METRICS.items():
            values = []
            for status, side in (("On", "ON"), ("Off", "OFF")):
                frequency = frequencies.loc[(source_row, frequency_metric, status)]
                attempt_rate = record[f"{attempt_prefix}_{side}"]
                values.append(attempt_rate * frequency)
            values_by_metric[metric] = tuple(values)
        for metric, (on_value, off_value) in values_by_metric.items():
            for status, value in (("On", on_value), ("Off", off_value), ("On-Off", on_value - off_value)):
                row = {column: base.get(column, pd.NA) for column in template_columns}
                row["metrics"] = metric
                row["Court Status"] = status
                row["PlayerValue"] = value
                row["LeagueAverage"] = pd.NA
                row["PlayerMinusLeague"] = pd.NA
                additions.append(row)

    result = pd.concat([master, pd.DataFrame(additions, columns=template_columns)], ignore_index=True)
    result.to_csv(output_path, index=False, na_rep="")
    return {
        "input_master_rows_after_rebuild_filter": len(master),
        "input_volume_rows": len(volume_raw),
        "unique_volume_player_team_seasons": len(volume),
        "new_metric_rows": len(additions),
        "output_rows": len(result),
        "output_metrics": int(result["metrics"].nunique()),
        "duplicate_volume_rows_removed": int(volume_raw.duplicated(KEYS).sum()),
        "refreshed_fta_player_rows": int(fta_mask.sum()),
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
