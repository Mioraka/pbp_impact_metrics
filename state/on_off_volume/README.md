# Resumable on/off volume collection

[Current status](STATUS.md) · [Workflow](../../actions/workflows/continue-on-off-volume.yml)

The checkpoint now resides in the repository at 470/2,274 requests, 207 completed batches, 379 team-season pairs. Existing successful responses are reused on resume.

`input.csv` retains only the original master's identity fields and team/opponent on/off 3PAr rows. It represents the same 566 source player rows and 379 team-season pairs as the source CSV. It is frozen for this collection; do not substitute a different master mid-run.

Six API stats per pair: OffPoss, DefPoss, FGA, FTA, OpponentFGA, OpponentFTA. Minutes come from response metadata. Team and opponent FTR are FTA/FGA. Two/three-point rates use the master's 3PAr shares; these derived values inherit its rounding. All calls use regular season data.

Resume begins at batch size 4 and wait 5 seconds. Success increases the next batch size by one (capped at 9) and reduces wait by 5 seconds (floor 0). A first failed pass holds batch size; a second consecutive failed pass reduces it by 1. Every failed pass adds 5 seconds wait before retry. After seven failed passes, the run stops and a scheduled run resumes using saved responses.

Status is committed after every ten completed batches and on run boundaries/errors. Every request failure appears in live Actions logs. The schedule attempts recovery every 30 minutes; GitHub may delay scheduled jobs. A concurrency group prevents overlapping collectors. Each Actions job runs up to 50 minutes. Failed segments restart inside the same job from the checkpoint. If the job window expires, the next scheduled run continues from that checkpoint. Use **Run workflow** for manual resume. Completion publishes `data/player_on_off_volume.csv`; the workflow subsequently exits without making API requests.

Run locally with Python 3.12: `python scripts/continue_on_off_volume.py`. Outside Actions it saves state without committing or pushing. The compact input is for collection only; join the final CSV back to the original master using source_row plus player/season/team identity fields.
