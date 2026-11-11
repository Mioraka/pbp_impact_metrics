# On/off volume collection

Status: **awaiting checkpoint transfer**

Requests: **456/2,274 (20.1%)**

Completed batches: **203**

Next batch size: **2**; added wait: **0 seconds**.

Updated (UTC): 2026-11-11T03:29:59.668858+00:00

[Workflow run](local)

Each successful batch increases concurrency by one, up to 9. The first failed pass holds concurrency; a second failed pass lowers it by one. Successful responses are retained.

The workflow checkpoints every 10 completed batches (and on errors or time limits). Live logs show every batch and request failure. Scheduled runs resume from the checkpoint.

The collector is paused until transfer of the saved response archive is approved. The workflow checks for that archive before making requests.
