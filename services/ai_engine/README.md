# AI Gateway

The service accepts `POST /analyze` requests and subscribes to `signals.pattern`.
It uses Instructor with OpenAI or Anthropic and validates all provider output
against the strict `AIAnalysisResult` model before returning or publishing it.
Pattern-event results go to `signals.ai`.

The circuit opens after three consecutive provider, timeout, or schema failures
by default, publishes `ai_state=OFFLINE` on `ai.state`, blocks further provider
calls for five minutes, then permits one half-open recovery probe. The circuit
threshold, recovery interval, timeout, and repeated OFFLINE publication cadence
are configurable in `.env.example`.

A token bucket defaults to five immediately available calls and one token of
refill per second. AI errors return a structured `NO_TRADE` result with a zero
risk multiplier. A deterministic quantitative override is honored only when
`AI_QUANT_OVERRIDE_ENABLED=true`, the request explicitly enables the override,
and it supplies all three override fields. Treat the override producer and
transport as trusted internal components.

Set `AI_PROVIDER=openai` or `anthropic`, `AI_MODEL`, and a provider key. The
service accepts `AI_API_KEY` as the provider key, or provider-specific
`OPENAI_API_KEY` / `ANTHROPIC_API_KEY`. Keep real credentials in an untracked
deployment secret store, not in the checked-in example.
