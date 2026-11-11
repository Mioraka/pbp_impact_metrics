# On/off volume collection

Status: **running**

Requests: **456/2,274 (20.1%)**

Completed batches: **203**

Next batch size: **2**; added wait: **0 seconds**.

Updated (UTC): 2026-11-11T03:34:17.482545+00:00

[Workflow run](https://github.com/Mioraka/pbp_impact_metrics/actions/runs/42953618174)

Each successful batch increases concurrency by one, up to 9. The first failed pass holds concurrency; a second failed pass lowers it by one. Successful responses are retained.

The workflow checkpoints every 10 completed batches (and on errors or time limits). Live logs show every batch and request failure. Scheduled runs resume from the checkpoint.
