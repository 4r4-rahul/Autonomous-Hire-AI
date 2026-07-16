"""
Single LLM wrapper for all agents.
All Anthropic API calls in the system go through here — retries, logging, cost tracking.
"""

import time
import asyncio
from typing import Any
import anthropic
from anthropic import AsyncAnthropic
from core.config import get_settings
from core.logger import get_logger

logger = get_logger("llm")


_total_cost_usd: float = 0.0
_total_calls: int = 0


def get_total_cost() -> float:
    return round(_total_cost_usd, 6)


def get_total_calls() -> int:
    return _total_calls


# Per-model costs in USD per million tokens
_MODEL_COSTS: dict[str, dict[str, float]] = {
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5": {"input": 0.80, "output": 4.00, "cache_write": 1.00, "cache_read": 0.08},
}
_DEFAULT_COSTS = _MODEL_COSTS["claude-opus-4-8"]


class LLMClient:
    """Async Anthropic client with retries, structured logging, and cost tracking."""

    def __init__(self, agent_name: str = "unknown", model: str | None = None):
        self._agent = agent_name
        cfg = get_settings()
        self._client = AsyncAnthropic(api_key=cfg.anthropic_api_key)
        self._model = model or cfg.anthropic_model

    async def chat(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 4096,
        temperature: float = 0.3,
        use_cache: bool = True,
        retries: int = 3,
    ) -> str:
        """Send a chat request. Returns the text content of the first message block."""
        global _total_cost_usd, _total_calls

        # Hard daily spend cap — restrains EXPENSIVE SPECULATIVE work only. Exemptions:
        #   • USER-TRIGGERED apply-time work — on-demand tailoring AND autofill (drafting answers
        #     while the user fills a real application form). These are the "real one" signal and must
        #     NEVER be blocked, or the user opens a form and the LLM answers come up blank.
        #   • cheap Haiku calls — discovery scoring, fit scoring, email classification (pennies each,
        #     and the core discovery loop). Capping these froze the whole pipeline once an Opus-heavy
        #     day hit the limit. Only Opus-tier SPECULATIVE/scheduled work is gated.
        from core import budget_guard
        _exempt = self._agent in ("on_demand_tailor", "autofill", "mock_interviewer")
        _cheap = "haiku" in (self._model or "").lower()
        if not _exempt and not _cheap and budget_guard.over_budget():
            logger.warning("llm_daily_budget_exceeded", agent=self._agent,
                           spent=round(budget_guard.spent_today(), 2), cap=budget_guard.daily_cap())
            raise budget_guard.BudgetExceededError(
                f"daily LLM budget ${budget_guard.daily_cap()} reached "
                f"(spent ${budget_guard.spent_today():.2f}) — skipping until tomorrow")

        system_param: list | str
        if use_cache:
            # Cache the system prompt — large, rarely changes, saves ~90% cost on repeated calls
            system_param = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        else:
            system_param = system

        # Opus 4.7+ removed temperature/top_p/top_k — passing them returns 400
        _no_sampling = "opus-4-7" in self._model or "opus-4-8" in self._model

        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                t0 = time.monotonic()
                params: dict = dict(
                    model=self._model,
                    max_tokens=max_tokens,
                    system=system_param,
                    messages=messages,
                )
                if not _no_sampling:
                    params["temperature"] = temperature
                response = await self._client.messages.create(**params)
                elapsed = time.monotonic() - t0

                # Cost tracking (per-model rates)
                usage = response.usage
                rates = _MODEL_COSTS.get(self._model, _DEFAULT_COSTS)
                cost = (
                    usage.input_tokens * rates["input"] / 1_000_000
                    + usage.output_tokens * rates["output"] / 1_000_000
                    + getattr(usage, "cache_creation_input_tokens", 0) * rates["cache_write"] / 1_000_000
                    + getattr(usage, "cache_read_input_tokens", 0) * rates["cache_read"] / 1_000_000
                )
                _total_cost_usd += cost
                _total_calls += 1
                budget_guard.record(cost)   # persist toward the daily cap

                # Append-only per-call cost ledger — survives process restarts and captures EVERY
                # call from EVERY process (scheduler, API, interactive), so spend is always fully
                # attributable by agent/model and a real leak is visible within hours.
                try:
                    import json as _json
                    from datetime import datetime as _dt, timezone as _tz
                    with open("data/llm_costs.jsonl", "a") as _f:
                        _f.write(_json.dumps({
                            "ts": _dt.now(_tz.utc).isoformat(),
                            "agent": self._agent, "model": self._model,
                            "in": usage.input_tokens, "out": usage.output_tokens,
                            "cache_read": getattr(usage, "cache_read_input_tokens", 0),
                            "cost": round(cost, 6),
                        }) + "\n")
                except Exception:
                    pass

                logger.log_decision(
                    action="llm_call",
                    outcome="success",
                    details={
                        "agent": self._agent,
                        "model": self._model,
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                        "cache_read_tokens": getattr(usage, "cache_read_input_tokens", 0),
                        "cost_usd": round(cost, 6),
                        "latency_s": round(elapsed, 2),
                    },
                )

                return response.content[0].text

            except anthropic.RateLimitError as e:
                wait = 2 ** attempt * 5  # 5s, 10s, 20s
                logger.warning("llm_rate_limit", attempt=attempt + 1, wait_s=wait)
                await asyncio.sleep(wait)
                last_error = e

            except anthropic.APIError as e:
                logger.error("llm_api_error", attempt=attempt + 1, error=str(e))
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)
                last_error = e

        logger.error("llm_all_retries_failed", agent=self._agent, error=str(last_error))
        raise RuntimeError(f"LLM call failed after {retries} retries: {last_error}") from last_error

    async def chat_json(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int = 4096,
        temperature: float = 0.2,
        retries: int = 3,
    ) -> dict | list:
        """Like chat() but parses the response as JSON. Adds JSON instruction to system prompt."""
        import json
        import re

        json_system = system + "\n\nIMPORTANT: Respond ONLY with valid JSON. No markdown, no explanation — raw JSON only."
        text = await self.chat(json_system, messages, max_tokens=max_tokens, temperature=temperature, retries=retries)

        # Strip markdown code fences if the model adds them despite instructions
        text = re.sub(r"^```(?:json)?\s*", "", text.strip())
        text = re.sub(r"\s*```$", "", text.strip())

        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            logger.error("llm_json_parse_error", agent=self._agent, raw=text[:300], error=str(e))
            raise ValueError(f"LLM returned invalid JSON: {e}\nRaw: {text[:300]}") from e


def get_llm(agent_name: str, model: str | None = None) -> LLMClient:
    return LLMClient(agent_name=agent_name, model=model)
