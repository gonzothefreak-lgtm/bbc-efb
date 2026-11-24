# R3.1 simulated coverage

| Area | Verified in the 31-case suite |
|---|---|
| Startup and transport | OPEN refusal, startup QUIT, dispatch-start exception, QUIT during dispatch startup and after healthy startup, fresh session demand replay. |
| Generations and data integrity | Old numeric and event callbacks rejected. Fast reconnect preserves the unread disconnect notice. First worker callback sees committed demand. |
| SimVars and reconciliation | Four ctypes datatypes, separate numeric and string delivery, shared demand and fanout, replacement, retirement, partial registration, interrupted staging, orphan callback rejection. |
| Freshness | Per-stream silence and recovery, stale read preserves its receipt timestamp, broker invalidation on loss. |
| Events | Event-only readiness and replay, unsupported filename-event demand and payload rejection, retained ordinary event delivery. |
| Teardown | Clean, repeated, pending and uncertain shutdown, hung close, cancellation and failed retirement. |
| Bounds and contention | Numeric, string, combined and bucket limits, bounded event and connection notice queues with gap accounting, bounded dispatch/reconcile and shutdown overlap. |
| Real wrapper and native behaviour | Unverified. Simulated workers cannot establish native lock ordering, timer races, close outcomes, packet age, DLL loading or simulator stability. |
