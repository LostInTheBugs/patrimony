# Token usage tracking — Patrimony

LLM token usage for this project, tallied session by session.

## Cumulative tally (2026-09-09)

| Metric | deepseek-v4-flash | gemini-3.6-flash (vision) | **Total** |
|---|---|---|---|
| Dev sessions (Hermes, interactive) | 6 | (same sessions) | **6** |
| Scripted agent sessions (API / docs) | 9 | 0 | **9** |
| Messages (exchanges) | — | — | **2 007** |
| API calls | 5 070 | 47 | **5 117** |
| Input tokens | 8 952 795 | 55 337 | **9 009 672** |
| Output tokens | 4 334 009 | 58 783 | **4 392 808** |
| **Subtotal (input + output)** | **13 286 804** | **114 120** | **13 402 480** |
| Cache read (reused at reduced price) | 1 048 377 472 | 0 | **1 048 377 472** |
| **Estimated cost** | **≈ 5.27 USD** | **≈ 0.46 USD** | **≈ 5.73 USD** |

(One free `nvidia/nemotron` call of 1 556 tokens is included in the totals;
the 9 scripted sessions (fiscal sheets, error-string translations) billed
< 0.01 USD.)

## How to re-read the counter

The Hermes session database (SQLite) holds the exact counters:

```sql
SELECT SUM(api_call_count), SUM(input_tokens), SUM(output_tokens),
       SUM(cache_read_tokens), SUM(estimated_cost_usd)
FROM session_model_usage
WHERE session_id IN (
  -- interactive dev sessions
  '20260904_172726_2a994788',  -- Patrimony bootstrap, first version (09-04)
  '20260905_103925_6fca4ce6',  -- LostInTheBugs/Patrimony review (09-05)
  '20260906_100900_f2e081d3',  -- dev + demo auto-reset (09-06)
  '20260907_065839_a7915f43',  -- build session (09-07)
  '20260908_101839_0f625177',  -- crowdfunding module integration (09-08)
  '20260909_071315_26cb4392',  -- dedicated pages / charts / simulators (09-09)
  -- scripted sessions (API): fiscal sheets FR/LU 2026 + consolidation
  '20260906_181909_1361c2','20260906_181909_49603a','20260906_181909_52e83d',
  '20260906_181909_87a87e','20260906_181909_9d48d5','20260906_182547_cd4e8a',
  -- scripted sessions (API): server error strings → EN/LU/DE (09-06)
  '20260906_222659_2bd8f3','20260906_222659_cf2a3d','20260906_222659_d5cbdf'
);
```

After each dev session, copy the matching row into the table above.

## Notes

- Tally taken from `~/.hermes/state.db` (table `session_model_usage`,
  filtered by session id) — real runtime counters, not an estimate.
- Covers every session attributed to the project since its first version
  (2026-09-04 bootstrap). Sessions run over Discord chat (no `cwd`) and
  were attributed by content (first user message + dominant mentions);
  a session can carry minor unrelated exchanges, so usage is slightly
  over-attributed. The 9 scripted sessions wrote the 2026 fiscal sheets
  (kept under `work/patrimony`, never pushed) and translated the server
  error strings of this app.
- The current 2026-09-09 session is still running: its last tours of the
  day (v062→v065) will land on the next tally once flushed.
- `reasoning_tokens` is probably included in `output_tokens`
  (to be confirmed with the provider).
