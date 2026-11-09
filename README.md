# NBA player-season on/off metrics

All available team on/off statistics for individual NBA players from the [PBP Stats API](https://api.pbpstats.com/docs).

## Scope and row definition

- Seasons: **2000-01 through 2026-27**, the NBA historical coverage advertised by [PBP Stats](https://www.pbpstats.com/).
- Regular Season, Playoffs, and PlayIn are separate. PlayIn discovery starts in 2019-20. Future postseason partitions can return zero teams/players.
- One output row per **league + season + season_type + player_id + team_id**.
- Players who changed teams have separate rows. Their team-relative rates must not be added or averaged to construct a combined row.
- Candidate players come from the compact team-roster endpoint. A nonzero ON statistic establishes participation. All-zero ON rows and initial request failures are checked against team-filtered player totals; DNPs are excluded and appearances shorter than one minute are retained.
- The current season is a snapshot at extraction time; historical coverage reflects the API, which can differ from official NBA totals.

## Published files

| File | Contents |
|---|---|
| `data/player_season_on_off.csv` | Combined wide results: identifiers plus on/off/difference columns for every returned metric |
| `data/by_season/YYYY-YY.csv` | Identical columns, partitioned by season for easier download |
| `data/queries.csv` | Exact request URLs, query dimensions, attempts, record counts, success/failure, timestamps, and cache use |
| `data/player_inventory.csv` | Discovered player-season-team-season_type keys and playing time |
| `data/coverage.csv` | Expected/downloaded rows and unique-player counts per season and season type |
| `data/metric_dictionary.csv` | Original API metric labels mapped to CSV columns |
| `data/failures.csv` | Requests that still failed after retries; header only when none remain |
| `data/run_summary.json` | Extraction status, scope, counts, errors, and timestamps |

The main results CSV is written only after the entire requested pull succeeds. A failed pull writes `*.partial.csv` locally and exits nonzero. Check `run_summary.json` for extraction status.

## Query strategy and the 500-record concern

1. For each individual season and season type, query `/get-totals/nba` with `Type=Team` to discover historical teams and their historical abbreviations.
2. Query the lightweight `/get-team-players-for-season` separately for each team. This returns only player IDs and names. Full team-filtered player totals are requested on demand for ambiguous participation or an initial failed player request, with one shared response per affected team.
3. For every discovered player, request `/get-on-off/nba/team` with **one season, one season type, one team, and one player**. All available metrics arrive together in `results`, one record per metric.
4. Pivot those metrics into one wide player-season-team-season_type row. Missing API metrics are blank; API zero values remain zero.

The OpenAPI documentation explicitly states a 500-possession limit for `/get-possessions/nba`. It does not document that limit for the on/off or totals endpoints. Nevertheless, this extractor partitions player discovery by team and rejects any discovery partition with 500 or more records to guard against possible truncation. A season's 400-600+ players are never treated as a single capped player batch. These endpoints expose no documented `page` or `offset` pagination parameter.

Example query:

```text
https://api.pbpstats.com/get-on-off/nba/team?Season=2026-27&SeasonType=Regular+Season&TeamId=1610612759&PlayerId=1641705
```

This returns San Antonio's statistics with and without Victor Wembanyama. The `/player` variant describes teammates' individual statistics and is not used here.

## Reproduce or resume

```bash
python -m pip install -r requirements.txt
python -m unittest -v
python pull_on_off.py
```

Defaults: 2000-01 through 2026-27, all three season types, roster discovery, 4 concurrent workers, up to 7 attempts per player metric request (including the initial eligibility probe), 15-second connect timeout and 60-second read timeout. Use `--discovery full` for the original discovery strategy.

```bash
# Smaller run in a separate output directory
python pull_on_off.py --start-year 2026 --end-year 2026 \
  --season-types 'Regular Season' --output sample_data

# Reuse validated checkpoints after interruption
python pull_on_off.py

# Fetch a new snapshot, including updates to the current season
python pull_on_off.py --refresh
```

Successful JSON responses are validated and saved as compressed local checkpoints under `.cache/`. Retrying or resuming reuses successful responses and downloads the missing ones. Cached timestamps are preserved. Use `--refresh` or a new `--cache` directory to update a completed snapshot.

Retries cover HTTP 429, server errors, connection failures, timeouts, invalid JSON, and invalid response structure. Backoff grows exponentially with jitter and honors `Retry-After`; permanent client errors fail without repeated requests. Exhausted failures are recorded and prevent publication of a complete results CSV.

## GitHub Actions

Automatic full-history runs are disabled while the cheaper extraction is validated. The [one-season test](https://github.com/Mioraka/pbp_impact_metrics/actions/workflows/test-one-season.yml) pulls only 2000-01, using a fresh cache and 12 workers, and requires all 681 previously identified player/team/season-type rows and all 89 metrics per row. It publishes result artifacts and per-request timings without launching a historical matrix.

The existing [full extraction workflow](https://github.com/Mioraka/pbp_impact_metrics/actions/workflows/pull-on-off.yml) is manual only. It runs one job per season, at most three season jobs at a time, with four request workers each. Each job makes up to three resumable passes and saves successful checkpoints even if it fails. Failed jobs can be rerun without downloading completed historical requests again.

After all 27 season jobs succeed, `combine_results.py` validates counts, keys, season coverage and failure audits, combines their CSV files, computes SHA-256 checksums, and commits the results under `data/`. Data commits do not retrigger extraction. The workflow is manually rerunnable; it is not scheduled.

Until that workflow succeeds, the complete CSV is unavailable. Successful individual-season results and failure audits are available in workflow artifacts. A running or failed workflow must not be interpreted as a complete dataset.

## NBA All-NBA and All-Defensive selections

The `Test NBA award data pull for 2025-26` GitHub Actions workflow validates the NBA Stats `PlayerAwards` endpoint against the official 2025-26 All-NBA and All-Defensive selections. On success, it publishes `data/player_award_selections_2025-26.csv` with one row per selection and the player, season, team, player ID, team ID, award, team designation, API description, and source URL. Players selected to both teams have two rows. This is a last-season validation file, not the all-time selection list.

The NBA Stats endpoint is player-specific. The test looks up IDs with `CommonAllPlayers`, then queries `PlayerAwards` once per unique candidate. It checks all 25 expected selections across 23 players before publishing. The candidate list is transcribed from the NBA's year-by-year All-NBA and All-Defensive pages.

## Interpretation

- `_on`: team metric with the selected player on court.
- `_off`: team metric with the selected player off court.
- `_diff`: API-supplied on-minus-off value. This can differ slightly from subtracting displayed values because of rounding.
- Percentages commonly use fractions (`0.553` = 55.3%). Some rates use other scales; retain the metric's original meaning from PBP Stats.
- On/off statistics describe lineup context and association, without adjustment for teammates, opponents, or substitution patterns.
- Numeric values preserve the API's displayed precision. Blank cells indicate a metric absent from that response.
- `games_played` and `seconds_played` are populated only when player totals were fetched. They remain blank for ordinary roster-discovered rows. The on/off endpoint does not return denominators or on/off minutes; they must not be inferred from a percentage.

Source: [API documentation](https://api.pbpstats.com/docs), [OpenAPI specification](https://api.pbpstats.com/openapi.json), and [PBP Stats](https://www.pbpstats.com/). This repository is an independent extraction, not an official NBA or PBP Stats publication.
