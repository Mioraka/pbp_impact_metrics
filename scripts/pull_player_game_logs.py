"""Download NBA regular-season and playoff player game logs to one Parquet file."""

import argparse
import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.compute as pc
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


def request_player_logs(season, season_type, start=None, end=None):
    return request_logs("leaguegamelog", {
        "Counter": 0,
        "DateFrom": nba_date(start),
        "DateTo": nba_date(end),
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


def nba_date(value):
    return value.strftime("%m/%d/%Y") if value else ""


def fetch_team_ratings(season, season_type, cache_dir, current_season, start=None, end=None):
    """Return NBA Stats advanced ratings indexed by game and team."""
    if int(season[:4]) < FIRST_ADVANCED_SEASON:
        return {}
    cache_path = (
        cache_dir / f"{season}-{season_type.lower().replace(' ', '-')}-advanced.json"
        if cache_dir is not None else None
    )
    use_cache = cache_path is not None and cache_path.exists() and season != current_season
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
                      Season=season, SeasonType=season_type,
                      DateFrom=nba_date(start), DateTo=nba_date(end))
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
    if cache_path is not None and not use_cache:
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


def season_start_year(day):
    return day.year if day.month >= 10 else day.year - 1


def season_label(year):
    return f"{year}-{(year + 1) % 100:02d}"


def date_window(table, start, end):
    return table.filter(pc.and_(pc.greater_equal(table["game_date"], start),
                                pc.less_equal(table["game_date"], end)))


def game_teams(table):
    return set(zip(table["game_id"].to_pylist(), table["team_id"].to_pylist(), strict=True))


def refresh_player_game_logs(output, today, stage=None):
    """Backfill missing dates and replace the most recent three days plus today."""
    existing = pq.read_table(output)
    if existing.schema != SCHEMA or not existing.num_rows:
        raise ValueError("Expected a populated player game log archive with the current schema")
    latest = pc.max(existing["game_date"]).as_py()
    if latest > today:
        raise ValueError(f"Archive ends on {latest}, after refresh date {today}")
    start = min(latest + timedelta(days=1), today - timedelta(days=3))
    refreshed = []
    for year in range(season_start_year(start), season_start_year(today) + 1):
        season = season_label(year)
        for season_type in SEASON_TYPES:
            payload = request_player_logs(season, season_type, start, today)
            players = date_window(parse_logs(payload, season, season_type, {}), start, today)
            if not players.num_rows:
                continue
            ratings = fetch_team_ratings(season, season_type, None, season, start, today)
            teams = game_teams(players)
            if any(key not in ratings or None in ratings[key] for key in teams):
                raise ValueError(f"{season} {season_type}: official team ratings are incomplete")
            if len(teams) != 2 * len({game_id for game_id, _ in teams}):
                raise ValueError(f"{season} {season_type}: player game logs are incomplete")
            refreshed.append(date_window(parse_logs(payload, season, season_type, ratings), start, today))

    replacement = pa.concat_tables(refreshed) if refreshed else SCHEMA.empty_table()
    if stage is not None:
        buffer = pa.BufferOutputStream()
        pq.write_table(replacement, buffer, compression="zstd")
        manifest = {
            "archive_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "start": start.isoformat(),
            "end": today.isoformat(),
            "parquet": base64.b64encode(buffer.getvalue()).decode("ascii"),
        }
        atomic_write(stage, lambda path: path.write_text(json.dumps(manifest) + "\n"))
        return
    publish_refresh(output, existing, start, today, replacement)


def publish_refresh(output, existing, start, today, replacement):
    previous = date_window(existing, start, today)
    if game_teams(previous) - game_teams(replacement):
        raise ValueError("NBA Stats omitted previously published games in the refresh window")
    order = [(column, "ascending") for column in ("game_date", "game_id", "team_id", "player_id")]
    replacement = replacement.sort_by(order)
    print(f"{start} through {today}: {replacement.num_rows:,} player rows, "
          f"{len(game_teams(replacement)) // 2} games", flush=True)
    if previous.sort_by(order).equals(replacement):
        print("Archive is already up to date", flush=True)
        return
    result = pa.concat_tables([existing.filter(pc.less(existing["game_date"], start)), replacement])
    atomic_write(output, lambda path: pq.write_table(result, path, compression="zstd"))
    print(f"Wrote {result.num_rows:,} player-game rows through "
          f"{pc.max(result['game_date']).as_py()} to {output}", flush=True)


def apply_staged_refresh(output, stage):
    manifest = json.loads(stage.read_text())
    archive = output.read_bytes()
    if hashlib.sha256(archive).hexdigest() != manifest["archive_sha256"]:
        raise ValueError("Archive changed after preparation. Prepare a fresh refresh.")
    start = date.fromisoformat(manifest["start"])
    end = date.fromisoformat(manifest["end"])
    replacement = pq.read_table(pa.BufferReader(base64.b64decode(manifest["parquet"], validate=True)))
    if replacement.schema != SCHEMA or not date_window(replacement, start, end).equals(replacement):
        raise ValueError("Invalid staged game log data")
    publish_refresh(output, pq.read_table(pa.BufferReader(archive)), start, end, replacement)


def pull_player_game_logs(output, cache_dir, start_year=1979, end_year=None, workers=4):
    """Fetch each season type, resume historical requests from cache, and write Parquet."""
    today = datetime.now(ZoneInfo("America/Toronto")).date()
    current_year = season_start_year(today)
    end_year = current_year if end_year is None else end_year
    if not 1979 <= start_year <= end_year <= current_year:
        raise ValueError(f"Expected 1979 <= start year <= end year <= {current_year}")

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
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--incremental", action="store_true", help="Refresh the published archive")
    mode.add_argument("--stage-refresh", type=Path, help="Prepare a refresh for publication")
    mode.add_argument("--apply-refresh", type=Path, help="Apply a prepared refresh")
    args = parser.parse_args()
    if args.apply_refresh:
        apply_staged_refresh(args.output, args.apply_refresh)
    elif args.incremental or args.stage_refresh:
        today = datetime.now(ZoneInfo("America/Toronto")).date()
        refresh_player_game_logs(args.output, today, args.stage_refresh)
    else:
        pull_player_game_logs(args.output, args.cache_dir, args.start_year, args.end_year, args.workers)
