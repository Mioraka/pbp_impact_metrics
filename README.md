# PBP Impact Stats: player and league advanced metrics

This repository's active workflow builds a player-level on/off table, league-season baselines, and a joined master table for the 89-metric PBPStats contract.

## Inputs and metric scope

- `data/player_list.csv` defines the player-season-team rows. It currently contains 518 historical award-selection rows spanning 2000-01 through 2024-25. Duplicate player-season-team rows are retained when they represent different award selections; API requests are deduplicated and results are copied back to each source row.
- Each player request uses PBPStats `/get-on-off/nba/team` with `Season`, `SeasonType=Regular Season`, `TeamId`, and `PlayerId`. A response must contain exactly the 89 metric labels in `data/metric_definitions.csv`, each with `On`, `Off`, and `On-Off` values.
- League-season rows use `/get-totals/nba` once per season with `SeasonType=Regular Season`, `StartType=All`, `GroupBy=Season`, and `Type=Team`. The output maps PBPStats totals and documented project formulas to the same 89 metric labels. League defensive series are symmetric with league offensive series, as the API's league totals provide no separate opposing-team population.
- The latest published range is 2000-01 through 2025-26 for league averages. The 2025-26 season is a snapshot at extraction time. Player-list rows currently end in 2024-25.

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

The player output has `518 × 89 × 3 = 138,306` metric-status rows. The league output has `26 × 89 = 2,314` rows. The master preserves the player output grain and adds `LeagueAverage` and `PlayerMinusLeague`.

## Run

The GitHub Actions workflow runs on changes to the pipeline inputs/code and can also be started manually from **Actions → Build player and league 89-metric master → Run workflow**. It performs unit validation, pulls the requested outputs, checks full metric and join coverage, uploads a downloadable workflow artifact, and commits complete CSV outputs to `main`.

To run locally:

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/build_advanced_master.py
```

The API response cache is under `.cache/player-league-v2/`. Re-running locally resumes successful requests. Outputs are written only after the corresponding full extraction succeeds; the master is written only after every player metric has a matching league-season value.

## Retired extraction

The prior all-roster extractor (`pull_on_off.py`, `combine_results.py`) and its manual all-history / one-season workflows have been retired from the active Actions workflow. They remain in the repository history for reference. The active process is `scripts/build_advanced_master.py` and `.github/workflows/build-player-league-master.yml`.

## Interpretation and limits

- `On` and `Off` are team-level rates while the listed player is on or off court. They do not represent that player's individual rate.
- `On-Off` is the API's reported difference. It may vary slightly from subtracting rounded displayed values.
- League defensive metrics duplicate the corresponding league offensive metric (`league_symmetry`) because a league aggregate has no distinct opponent sample.
- Team trades remain separate player-team records. Team-relative on/off rates are not combined across stints.
- The current player list is an award-selection list, not a census of every NBA player.
- A failed run is incomplete. Check the failed-run log in the workflow output; incomplete results must not be treated as published coverage.

Data source: [PBPStats API documentation](https://api.pbpstats.com/docs). This repository is an independent extraction project, not an official NBA or PBPStats publication.
