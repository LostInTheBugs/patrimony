# Token usage tracking — Patrimony

LLM token usage for this project, tallied session by session.

## Cumulative tally (2026-09-09)

| Metric | deepseek-v4-flash | gemini-3.6-flash (vision) | **Total** |
|---|---|---|---|
| Dev sessions (Hermes) | 5 | (same sessions) | **5** |
| Messages (exchanges) | — | — | **1 524** |
| API calls | 3 831 | 28 | **3 859** |
| Input tokens | 6 813 675 | 32 892 | **6 848 107** |
| Output tokens | 3 202 604 | 35 459 | **3 238 079** |
| **Subtotal (input + output)** | **10 016 279** | **68 351** | **10 086 186** |
| Cache read (reused at reduced price) | 813 466 752 | 0 | **813 466 752** |
| **Estimated cost** | **≈ 4.12 USD** | **≈ 0.27 USD** | **≈ 4.39 USD** |

(One free `nvidia/nemotron` call of 1 556 tokens is included in the totals.)

## How to re-read the counter

The Hermes session database (SQLite) holds the exact counters:

```sql
SELECT SUM(api_call_count), SUM(input_tokens), SUM(output_tokens),
       SUM(cache_read_tokens), SUM(estimated_cost_usd)
FROM session_model_usage
WHERE session_id IN (
  '20260904_172726_2a994788',  -- Patrimony bootstrap (2026-09-04)
  '20260905_103925_6fca4ce6',  -- LostInTheBugs/patrimony review (09-05)
  '20260907_065839_a7915f43',  -- build session (09-07)
  '20260908_101839_0f625177',  -- crowdfunding module integration (09-08)
  '20260909_071315_26cb4392'   -- dedicated pages / charts / simulators (09-09)
);
```

After each dev session, copy the matching row into the table above.

## Notes

- Tally taken from `~/.hermes/state.db` (table `session_model_usage`,
  filtered by session id) — real runtime counters, not an estimate.
- Sessions run over Discord chat (no `cwd`); they were attributed to this
  project by content (first user message + dominant mentions). A session
  can carry minor unrelated exchanges, so usage is slightly over-attributed.
- The current 2026-09-09 session is still running: the last tours of the
  day (v062→v065) will land on the next tally once flushed.
- `reasoning_tokens` is probably included in `output_tokens`
  (to be confirmed with the provider).
