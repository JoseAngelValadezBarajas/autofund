# Adaptive learning boundary

The live `AdaptiveEngine` uses deterministic regime rules and may select only a `CERTIFIED` `StrategyProfile` or `DO_NOT_TRADE`. Initial production has one certified champion. High spread, poor liquidity and degraded data select `DO_NOT_TRADE`; no second profile is invented.

Live adaptation may skip work or apply certified cooldown/profile choices. It cannot change capital, deployment, the 11 MXN cap, RiskEngine invariants, kill behavior, accounting, exchange permissions or capabilities. It does not generate code and no LLM chooses BUY or SELL.

Challengers are `RESEARCH_ONLY`. Their fingerprints, data/evaluation evidence and disposition are stored in telemetry. Automatic promotion is disabled. Manual promotion is allowed only between sessions after replay, out-of-sample and shadow evidence; the champion is immutable during a live session.
