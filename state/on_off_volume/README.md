# Resumable on/off volume collection

[Current status](STATUS.md) · [Workflow](../../actions/workflows/continue-on-off-volume.yml)

The seed contains the actual local checkpoint: 456/2,274 requests, 203 completed batches, 379 team-season pairs. It includes all successful API response files, including any saved after the latest completed batch. Existing successful responses are reused on resume.

`input.csv` retains only the original master's identity fields and team/opponent on/off 3PAr rows. It represents the same 566 source player rows and 379 team-season pairs as the source CSV. It is frozen for this collection; do not substitute a different master mid-run.

Six API stats per pair: OffPoss, DefPoss, FGA, FTA, OpponentFGA, OpponentFTA. Minutes come from response metadata. Team and opponent FTR are FTA/FGA. Two/three-point rates use the master's 3PAr shares; these derived values inherit its rounding. All calls use regular season data.

Success increases the next batch size by one, capped at 9. One failed pass holds its size; the second failed pass lowers it by one (floor 1). That failure counter then resets; a successful batch also resets it. Failed requests retry immediately, with no added wait. After seven failed passes, the run stops with an error and the next scheduled run resumes using saved responses.

Status is committed after every ten completed batches and on run boundaries/errors. Every request failure appears in live Actions logs. The schedule attempts recovery every 30 minutes; GitHub may delay scheduled jobs. A concurrency group prevents overlapping collectors. Runs last at most 45 minutes of collection, then checkpoint for the next run. Use **Run workflow** for manual resume. Completion publishes `data/player_on_off_volume.csv`; the workflow subsequently exits without making API requests.

Run locally with Python 3.12: `python scripts/continue_on_off_volume.py`. Outside Actions it saves state without committing or pushing. The compact input is for collection only; join the final CSV back to the original master using source_row plus player/season/team identity fields.
