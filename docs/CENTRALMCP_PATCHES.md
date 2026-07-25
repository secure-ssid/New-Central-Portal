# centralmcp local patches

`centralmcp` is a separate upstream project, mounted into the app container
read-only at `/centralmcp` from `$CENTRALMCP_PATH` on the host. Upstream
force-pushes `main`, so **every patch below is lost on update** and has to be
re-applied after:

```bash
cd "$CENTRALMCP_PATH" && git fetch && git checkout -B main origin/main
```

Each patch that can be defended from the portal side also has a runtime
override in the portal, noted below. Check those still log as expected after an
update — if the override warns, upstream moved the symbol.

---

## 1. 429 rate-limit backoff floor: 60s → 5s

**File:** `pipeline/clients/central_client.py`

```python
-_INITIAL_RETRY_DELAY = 60  # seconds — Central rate-limit window
+_INITIAL_RETRY_DELAY = 5   # seconds — first 429 backoff, grows 1.5x per attempt
```

**Why.** Central returns HTTP 429 without a `Retry-After` header, so this floor
is what every rate-limited call actually costs. The handler sleeps *blocking*
(`time.sleep`) in a thread-pool worker, on a single-worker uvicorn. Measured on
the portal before the fix: three consecutive dashboard loads took 3.7s, 63.4s
and 123.8s — exactly zero, one and two 60s sleeps. Nine 429s were recorded in a
single hour.

The constant feeds both the sync path (`_request`) and the async path
(`_arequest`); both read it at call time, so the one edit covers both. The 1.5x
growth and the `_MAX_RETRY_DELAY = 300` ceiling are unchanged, so a genuinely
throttled client still backs off to the same place — it just gets there
gradually instead of paying a minute up front.

**Portal-side override:** `_tame_centralmcp_rate_limit_backoff()` in
`app/main.py`, called from the lifespan startup hook. It rebinds the module
attribute if it is still larger than the target, and logs
`Central 429 backoff floor lowered 60s → 5s`. Tune with the
`CENTRAL_RATE_LIMIT_INITIAL_DELAY` env var. This means **the portal is safe even
if this patch is lost** — re-apply it anyway so anything else using centralmcp
directly (the MCP server, CLI tools) benefits too.

**Verify after an update:**
```bash
grep -n '_INITIAL_RETRY_DELAY = ' "$CENTRALMCP_PATH/pipeline/clients/central_client.py"
docker logs netlab-app 2>&1 | grep 'backoff floor'
```

---

## Related, not patched here

The portal reduces 429s at the source rather than only handling them: the
60s TTL cache with in-flight coalescing in `app/vendors/central_bridge.py` cut
upstream traffic from ~711 calls/hour to a fraction of that. If 429s reappear,
check that cache first — a rate limit is a symptom of call volume, and the
backoff change only makes the symptom cheaper.
