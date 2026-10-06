"""The run record's vocabulary, shared by ingest, prune, queries and the dashboard. Pure.

`ingest_runs.status` keeps meaning the fetch outcome: running | succeeded | partial | failed.
"Good" is stricter: the fetch succeeded and no error-level check failed. Only good runs set the
volume baseline, freshness, what counts as listed, and the run whose raw prune keeps.
"""

from __future__ import annotations

from datetime import timedelta

# Runs from before Phase 5 ran no checks (checks_failed is NULL); they count as good.
GOOD_RUN = "status = 'succeeded' AND coalesce(checks_failed, 0) = 0"

# The workflow's timeout-minutes (.github/workflows/ingest.yml). A run still "running" after this
# was killed, timed out or lost its database connection, so the next ingest closes it.
ABANDONED_AFTER = timedelta(minutes=10)
ABANDONED = "abandoned:"  # how a closed abandoned run's error begins
