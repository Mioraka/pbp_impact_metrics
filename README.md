# PBP Impact Stats: player and league advanced metrics

This repository's active workflow builds a player-level on/off table, league-season baselines, and a joined master table for the 89-metric PBPStats contract.

## Inputs and metric scope

- `data/player_list.csv` defines the player-season-team rows. It currently contains 566 rows: 518 historical award-selection rows spanning 2000-01 through 2024-25, plus 48 requested player/team rows for 2025-26 and 2026-27. Award flags are blank on the requested rows because they were added as a player sample rather than as award records. Duplicate player-season-team rows are retained when they represent different award selections; API requests are deduplicated and results are copied back to each source row.
- Each player request uses PBPStats `/get-on-off/nba/team` with `Season`, `SeasonType=Regular Season`, `TeamId`, and `PlayerId`. A response must contain exactly the 89 metric labels in `data/metric_definitions.csv`, each with `On`, `Off`, and `On-Off` values.
- League-season rows use `/get-totals/nba` once per season with `SeasonType=Regular Season`, `StartType=All`, `GroupBy=Season`, and `Type=Team`. The output maps PBPStats totals and documented project formulas to the same 89 metric labels. League defensive series are symmetric with league offensive series, as the API's league totals provide no separate opposing-team population.
- The workflow pulls league averages from 2000-01 through 2026-27, and player rows through 2026-27. The 2026-27 player and league values are a regular-season-to-date snapshot at extraction time.

## Published outputs

| File | Contents |
|---|---|
| `data/player_list.csv` | Source player and award-selection rows |
| `data/metric_definitions.csv` | The 89 required metric names and PBPStats source field/formula IDs |
| `data/player_advanced_metrics.csv` | Long-form player metrics: one row per source row, metric, and court status |
| `data/league_season_averages.csv` | One row per league season and metric |
| `data/player_league_master.csv` | Player metrics joined to the matching league-season average, including the player-minus-league difference |
| `data/run_manifest.json` | Completion state, row counts, season type, and join keys |
| `data/request_log.json` | Request URLs, statuses, retry counts, timings, cache use, and timestamps |

The player output has `566 × 89 × 3 = 151,122` metric-status rows. The league output has `27 × 89 = 2,403` rows. The master preserves the player output grain and adds `LeagueAverage` and `PlayerMinusLeague`.

## Run

The GitHub Actions workflow runs on changes to the pipeline inputs/code and can also be started manually from **Actions → Build player and league 89-metric master → Run workflow**. Push-triggered runs fetch only player rows missing from the published output and missing league seasons. A manual run refreshes 2026-27 player and league values. Each run checks full metric and join coverage, uploads a downloadable workflow artifact, and commits complete CSV outputs to `main`.

To run locally:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/build_advanced_master.py
```

The API response cache is under `.cache/player-league-incremental/`. Re-running with the same cache resumes successful requests. The active incremental process is `scripts/append_missing_master.py`; use `scripts/build_advanced_master.py` for a deliberate full rebuild. The master is written only after every player metric has a matching league-season value.

## Retired extraction

The prior all-roster extractor (`pull_on_off.py`, `combine_results.py`) and its manual all-history / one-season workflows have been retired from the active Actions workflow. They remain in the repository history for reference. The active workflow uses `scripts/append_missing_master.py` and `.github/workflows/build-player-league-master.yml`; `scripts/build_advanced_master.py` remains the full-rebuild implementation.

## Interpretation and limits

- `On` and `Off` are team-level rates while the listed player is on or off court. They do not represent that player's individual rate.
- `On-Off` is the API's reported difference. It may vary slightly from subtracting rounded displayed values.
- League defensive metrics duplicate the corresponding league offensive metric (`league_symmetry`) because a league aggregate has no distinct opponent sample.
- Team trades remain separate player-team records. Team-relative on/off rates are not combined across stints.
- The current player list is an award-selection list, not a census of every NBA player.
- A failed run is incomplete. Check the failed-run log in the workflow output; incomplete results must not be treated as published coverage.

Data source: [PBPStats API documentation](https://api.pbpstats.com/docs). This repository is an independent extraction project, not an official NBA or PBPStats publication.


## Daily combined refresh

The active workflow runs every morning at 07:00 America/Toronto (DST-aware UTC slots; GitHub may delay scheduled jobs), and on manual dispatch or pipeline changes. It runs `python scripts/refresh_current_season.py`. The season is inferred using an October rollover; `--season 2026-27` allows an explicit override locally. Players must already be listed in `data/player_list.csv`.

The process refreshes current-season 89-metric player data and league baselines, then fetches fresh FGA, FTA, opponent FGA/FTA, possessions and minutes using an isolated cache. It replaces current-season volume rows, retains historical volume, and rejoins 2PA/3PA, free-throw and zone attempt rates into the master. Only successful, validated results are committed together. Failures leave the published CSVs unchanged. Added volume metrics retain blank league baselines. The manifest reports final master counts after the volume join.

The older volume continuation workflow is retained for manual recovery only; its recurring schedule is removed. Both workflows share one concurrency group. A daily refresh requires the existing published base and volume files; missing historical volume coverage stops publication.
