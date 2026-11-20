"""Download NBA regular-season and playoff player game logs to one Parquet file."""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

NBA_STATS = "https://stats.nba.com/stats"
FIRST_ADVANCED_SEASON = 1996
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://www.nba.com/",
    "Accept": "application/json",
}
SEASON_TYPES = ("Regular Season", "Playoffs")
TEXT_COLUMNS = (
    "season", "season_type", "season_id", "player_name", "team_abbreviation",
    "team_name", "game_id", "matchup", "wl",
)
NUMBER_COLUMNS = (
    "min", "fg_pct", "fg3_pct", "ft_pct", "plus_minus", "fantasy_pts",
    "team_off_rating", "team_def_rating",
)
COUNT_COLUMNS = (
    "player_id", "team_id", "fgm", "fga", "fg3m", "fg3a", "ftm", "fta",
    "oreb", "dreb", "reb", "ast", "stl", "blk", "tov", "pf", "pts",
    "video_available",
)
SCHEMA = pa.schema(
    [("game_date", pa.date32())]
    + [(name, pa.string()) for name in TEXT_COLUMNS]
    + [(name, pa.int32()) for name in COUNT_COLUMNS]
    + [(name, pa.float32()) for name in NUMBER_COLUMNS]
)


def atomic_write(path, write):
    """Replace the destination only after the writer succeeds."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        write(temporary)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def request_player_logs(season, season_type):
    return request_logs("leaguegamelog", {
        "Counter": 0,
        "DateFrom": "",
        "DateTo": "",
        "Direction": "ASC",
        "LeagueID": "00",
        "PlayerOrTeam": "P",
        "Season": season,
        "SeasonType": season_type,
        "Sorter": "DATE",
    })


def request_logs(endpoint, params):
    query = urlencode(params)
    request = Request(f"{NBA_STATS}/{endpoint}?{query}", headers=HEADERS)
    for attempt in range(4):
        try:
            with urlopen(request, timeout=35) as response:
                return json.load(response)
        except (URLError, TimeoutError, ValueError, OSError) as error:
            if attempt == 3 or (
                isinstance(error, HTTPError) and error.code < 500 and error.code != 429
            ):
                raise RuntimeError(f"{endpoint} {params['Season']} {params['SeasonType']}: {error}") from error
            sleep(3 * 2**attempt)


def fetch_team_ratings(season, season_type, cache_dir, current_season):
    """Return NBA Stats advanced ratings indexed by game and team."""
    if int(season[:4]) < FIRST_ADVANCED_SEASON:
        return {}
    cache_path = cache_dir / f"{season}-{season_type.lower().replace(' ', '-')}-advanced.json"
    use_cache = cache_path.exists() and season != current_season
    if use_cache:
        payload = json.loads(cache_path.read_text())
    else:
        params = dict.fromkeys((
            "DateFrom", "DateTo", "GameSegment", "LastNGames", "LeagueID", "Location",
            "MeasureType", "Month", "OppTeamID", "Outcome", "PORound", "PerMode", "Period",
            "PlayerID", "Season", "SeasonSegment", "SeasonType", "ShotClockRange", "TeamID",
            "VsConference", "VsDivision",
        ), "")
        params.update(LeagueID="00", MeasureType="Advanced", PerMode="Totals",
                      Season=season, SeasonType=season_type)
        payload = request_logs("teamgamelogs", params)

    result = next(item for item in payload["resultSets"] if item["name"] == "TeamGameLogs")
    columns = result["headers"]
    if not result["rowSet"] or not {"OFF_RATING", "DEF_RATING"}.issubset(columns):
        return {}
    ratings = {}
    for values in result["rowSet"]:
        row = dict(zip(columns, values, strict=True))
        key = (row["GAME_ID"], row["TEAM_ID"])
        value = (row["OFF_RATING"], row["DEF_RATING"])
        if key in ratings and ratings[key] != value:
            raise ValueError(f"{season} {season_type}: conflicting team ratings {key}")
        ratings[key] = value
    if not use_cache:
        atomic_write(cache_path, lambda path: path.write_text(json.dumps(payload)))
    return ratings


def parse_logs(payload, season, season_type, ratings):
    result = next(item for item in payload["resultSets"] if item["name"] == "LeagueGameLog")
    columns = [name.lower() for name in result["headers"]]
    required = {"game_id", "game_date", "player_id", "team_id", "pts"}
    if not required.issubset(columns):
        raise ValueError(f"{season} {season_type}: missing columns {required - set(columns)}")
    accepted = {}
    for values in result["rowSet"]:
        row = dict(zip(columns, values, strict=True))
        if any(row[name] is None for name in required - {"pts"}):
            raise ValueError(f"{season} {season_type}: incomplete game or player ID")
        row["game_date"] = date.fromisoformat(row["game_date"])
        row["season"] = season
        row["season_type"] = season_type
        team_game = (row["game_id"], row["team_id"])
        row["team_off_rating"], row["team_def_rating"] = ratings.get(team_game, (None, None))
        # Some historical source records list the same player on both teams.
        key = (row["game_id"], row["player_id"], row["team_id"])
        if key in accepted and accepted[key] != row:
            raise ValueError(f"{season} {season_type}: conflicting player-game records {key}")
        accepted[key] = row
    return pa.Table.from_pylist(list(accepted.values()), schema=SCHEMA)


def fetch_season(season, season_type, cache_dir, current_season):
    cache_path = cache_dir / f"{season}-{season_type.lower().replace(' ', '-')}.json"
    use_cache = cache_path.exists() and season != current_season
    payload = json.loads(cache_path.read_text()) if use_cache else request_player_logs(season, season_type)
    ratings = fetch_team_ratings(season, season_type, cache_dir, current_season)
    table = parse_logs(payload, season, season_type, ratings)
    if not table.num_rows and (season != current_season or season_type != "Playoffs"):
        raise ValueError(f"{season} {season_type}: empty game log")
    if not use_cache and table.num_rows:
        atomic_write(cache_path, lambda path: path.write_text(json.dumps(payload)))
    rated = table.num_rows - table["team_off_rating"].null_count
    print(f"{season} {season_type}: {table.num_rows:,} rows, {rated:,} with team ORtg", flush=True)
    return table


def pull_player_game_logs(output, cache_dir, start_year=1979, end_year=None, workers=4):
    """Fetch each season type, resume historical requests from cache, and write Parquet."""
    today = datetime.now(ZoneInfo("America/Toronto")).date()
    current_year = today.year if today.month >= 10 else today.year - 1
    end_year = current_year if end_year is None else end_year
    if not 1979 <= start_year <= end_year <= current_year:
        raise ValueError(f"Expected 1979 <= start year <= end year <= {current_year}")

    def season_label(year):
        return f"{year}-{(year + 1) % 100:02d}"

    jobs = [
        (season_label(year), season_type)
        for year in range(start_year, end_year + 1)
        for season_type in SEASON_TYPES
    ]

    def fetch(job):
        return fetch_season(*job, cache_dir, season_label(current_year))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        tables = list(executor.map(fetch, jobs))
    result = pa.concat_tables(tables)
    atomic_write(output, lambda path: pq.write_table(result, path, compression="zstd"))
    print(f"Wrote {result.num_rows:,} player-game rows to {output}", flush=True)
    return result.num_rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-year", type=int, default=1979)
    parser.add_argument("--end-year", type=int)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output", type=Path, default=Path("data/nba_player_game_logs.parquet"))
    parser.add_argument("--cache-dir", type=Path, default=Path(".cache/nba-player-game-logs"))
    args = parser.parse_args()
    pull_player_game_logs(args.output, args.cache_dir, args.start_year, args.end_year, args.workers)
