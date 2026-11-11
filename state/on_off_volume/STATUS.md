# On/off volume collection

Status: **running**

Requests: **1,552/2,274 (68.2%)**

Completed batches: **330**

Next batch size: **9**; retry wait: **25.0 seconds** (maximum 30).

Updated (UTC): 2026-11-11T09:42:11.639916+00:00

[Workflow run](https://github.com/Mioraka/pbp_impact_metrics/actions/runs/42996535773)

Each successful batch increases concurrency by one, up to 9. Wait increases by 5 seconds after failure and decreases by 5 seconds after success, to a floor of 0. The first failed pass holds concurrency; a second failed pass lowers it by one. Successful responses are retained.

The workflow checkpoints every 10 completed batches (and on errors or time limits). Live logs show every batch and request failure. Scheduled runs resume from the checkpoint.
