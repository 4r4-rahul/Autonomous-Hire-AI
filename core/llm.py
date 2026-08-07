"""
Single LLM wrapper for all agents.
All Anthropic API calls in the system go through here — retries, logging, cost tracking.
"""

import time
import asyncio
from typing import Any
import anthropic
from anthropic import AsyncAnthropic
import openai
from openai import AsyncOpenAI
from core.config import get_settings
from core.logger import get_logger

logger = get_logger("llm")


_total_cost_usd: float = 0.0
_total_calls: int = 0


# ── Dead-key watchdog ──────────────────────────────────────────────────────
# A rotated/invalid API key on a long-running process (e.g. the launchd scheduler,
# whose env is frozen at launch) fails silently: scans keep running, but every LLM
# call 401s and nothing gets queued. This has bitten us twice for ~days at a time.
# Track CONSECUTIVE auth failures process-wide; once past a threshold, fire ONE
# Discord alert per cooldown window so a dead key surfaces in minutes, not days.
_AUTH_FAIL_STREAK = 0
_AUTH_ALERT_THRESHOLD = 5            # consecutive 401s before alerting
_AUTH_ALERT_COOLDOWN_S = 1800        # 30 min between alerts (401s can fire thousands/min)
_auth_last_alert_at: float = 0.0


def _is_auth_error(e: Exception) -> bool:
    """True for a 401/invalid-key error from either provider (Anthropic or OpenAI/DeepSeek)."""
    if isinstance(e, (anthropic.AuthenticationError, openai.AuthenticationError)):
        return True
    if getattr(e, "status_code", None) == 401:
        return True
    msg = str(e).lower()
    return "authentication_error" in msg or "api key is invalid" in msg or "invalid api key" in msg


def _note_auth_failure(agent: str, model: str, error: str) -> None:
    """Bump the consecutive-401 streak; alert once we cross the threshold (cooldown-gated)."""
    global _AUTH_FAIL_STREAK, _auth_last_alert_at
    _AUTH_FAIL_STREAK += 1
    if _AUTH_FAIL_STREAK < _AUTH_ALERT_THRESHOLD:
        return
    now = time.monotonic()
    if now - _auth_last_alert_at < _AUTH_ALERT_COOLDOWN_S:
        return
    _auth_last_alert_at = now
    logger.error("llm_auth_dead_key", agent=agent, model=model,
                 consecutive=_AUTH_FAIL_STREAK)
    try:
        settings = get_settings()
        webhook = settings.discord_webhook_digest or settings.discord_webhook_review
        if not webhook:
            return
        msg = (f"🔴 **Dead LLM API key** — {_AUTH_FAIL_STREAK} consecutive 401s "
               f"(last: agent `{agent}`, model `{model}`). The pipeline is discovering "
               f"roles but queuing ZERO. Likely a rotated key on the long-running scheduler "
               f"(env frozen at launch). Fix: update the key, then restart the scheduler:\n"
               f"`launchctl kickstart -k gui/$(id -u)/com.autonomoushire.scheduler`")

        async def _post():
            try:
                import httpx
                async with httpx.AsyncClient() as c:
                    await c.post(webhook, json={"content": msg[:1900]}, timeout=5)
            except Exception:
                pass
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(_post())
            else:
                loop.run_until_complete(_post())
        except Exception:
            pass
    except Exception as e:
        logger.warning("auth_alert_failed", error=str(e))


def _note_auth_success() -> None:
    """Any successful call clears the streak — the key is alive again."""
    global _AUTH_FAIL_STREAK
    if _AUTH_FAIL_STREAK:
        _AUTH_FAIL_STREAK = 0


def get_total_cost() -> float:
    return round(_total_cost_usd, 6)


def get_total_calls() -> int:
    return _total_calls


# Per-model costs in USD per million tokens
_MODEL_COSTS: dict[str, dict[str, float]] = {
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_write": 6.25, "cache_read": 0.50},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-sonnet-5": {"input": 3.00, "output": 15.00, "cache_write": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5": {"input": 0.80, "output": 4.00, "cache_write": 1.00, "cache_read": 0.08},
    # DeepSeek V4 (cache_read = cached-prefix rate; no separate cache-write charge)
    "deepseek-v4-pro": {"input": 0.435, "output": 0.87, "cache_write": 0.435, "cache_read": 0.03},
    "deepseek-v4-flash": {"input": 0.14, "output": 0.28, "cache_write": 0.14, "cache_read": 0.03},
}
_DEFAULT_COSTS = _MODEL_COSTS["claude-opus-4-8"]

# Tier detection: agents request Claude model names. "haiku" marks the cheap/high-volume tier
# (scoring, classification, intake); everything else is the quality tier (tailoring, cover, prep).
_CHEAP_TIER_HINTS = ("haiku", "flash")

# In "auto" thinking mode, only reason when the answer budget can absorb reasoning tokens without
# truncating the content (tight-budget calls like short summaries would come back empty otherwise).
_THINK_MIN_TOKENS = 1024
# When thinking is enabled, add this many tokens on top of the caller's budget for the reasoning
# trace, so the answer isn't truncated (max_tokens caps reasoning + answer combined).
_REASONING_HEADROOM = 3072


def _is_cheap(requested_model: str) -> bool:
    return any(h in (requested_model or "").lower() for h in _CHEAP_TIER_HINTS)


def _resolve_model(requested_model: str, provider: str, cfg) -> str:
    """Map a requested Claude model name to the actual model id for the active provider, preserving
    tier (cheap -> flash, quality -> pro). Requesting a native deepseek id passes through."""
    if provider != "deepseek":
        return requested_model
    if requested_model.startswith("deepseek"):
        return requested_model
    return cfg.deepseek_model_flash if _is_cheap(requested_model) else cfg.deepseek_model_pro


class LLMClient:
    """Async LLM client (Anthropic or DeepSeek/OpenAI-compatible) with retries, logging, cost tracking."""

    def __init__(self, agent_name: str = "unknown", model: str | None = None):
        self._agent = agent_name
        cfg = get_settings()
        self._think_mode = str(getattr(cfg, "deepseek_thinking", "auto")).lower()
        # The Claude-style name the agent asked for — drives tier logic and budget exemptions.
        self._requested = model or cfg.anthropic_model
        self._cheap = _is_cheap(self._requested)
        # Per-tier provider: the QUALITY tier (résumé tailoring / cover / interview-prep / fix-loop)
        # can run on a different provider than the cheap high-volume tier. LLM_QUALITY_PROVIDER lets
        # the money-path use Claude (which follows the nuanced résumé rules reliably) while scoring/
        # classification/intake stay on cheap DeepSeek. Empty → follow LLM_PROVIDER for all tiers.
        provider = (cfg.llm_provider or "anthropic").lower()
        qp = str(getattr(cfg, "llm_quality_provider", "") or "").lower()
        if not self._cheap and qp:
            provider = qp
        self._provider = provider
        # The concrete model id actually sent to the provider.
        self._model = _resolve_model(self._requested, self._provider, cfg)
        if self._provider == "deepseek":
            self._key_missing = not cfg.deepseek_api_key
            # Construct with a placeholder if the key isn't set yet so get_llm() never crashes on
            # import; chat() raises a clear error instead of a bare 401.
            self._client = AsyncOpenAI(
                api_key=cfg.deepseek_api_key or "MISSING_DEEPSEEK_KEY",
                base_url=cfg.deepseek_base_url,
            )
        else:
            self._key_missing = not cfg.anthropic_api_key
            self._client = AsyncAnthropic(api_key=cfg.anthropic_api_key or "MISSING_ANTHROPIC_KEY")

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

        if self._key_missing:
            raise RuntimeError(
                f"LLM_PROVIDER={self._provider} but its API key is not set in .env "
                f"({'DEEPSEEK_API_KEY' if self._provider == 'deepseek' else 'ANTHROPIC_API_KEY'})")

        # Hard daily spend cap — restrains EXPENSIVE SPECULATIVE work only. Exemptions:
        #   • USER-TRIGGERED apply-time work — on-demand tailoring AND autofill (drafting answers
        #     while the user fills a real application form). These are the "real one" signal and must
        #     NEVER be blocked, or the user opens a form and the LLM answers come up blank.
        #   • cheap Haiku calls — discovery scoring, fit scoring, email classification (pennies each,
        #     and the core discovery loop). Capping these froze the whole pipeline once an Opus-heavy
        #     day hit the limit. Only Opus-tier SPECULATIVE/scheduled work is gated.
        from core import budget_guard
        _exempt = self._agent in ("on_demand_tailor", "autofill", "mock_interviewer")
        _cheap = self._cheap
        if not _exempt and not _cheap and budget_guard.over_budget():
            logger.warning("llm_daily_budget_exceeded", agent=self._agent,
                           spent=round(budget_guard.spent_today(), 2), cap=budget_guard.daily_cap())
            raise budget_guard.BudgetExceededError(
                f"daily LLM budget ${budget_guard.daily_cap()} reached "
                f"(spent ${budget_guard.spent_today():.2f}) — skipping until tomorrow")

        rates = _MODEL_COSTS.get(self._model, _DEFAULT_COSTS)
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                t0 = time.monotonic()

                if self._provider == "deepseek":
                    # OpenAI-compatible chat completion. DeepSeek auto-caches shared prompt prefixes
                    # (no cache_control needed); usage reports cache hit/miss split for costing.
                    full_messages = [{"role": "system", "content": system}] + list(messages)
                    ds_params: dict = dict(
                        model=self._model,
                        messages=full_messages,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    )
                    # Thinking improves quality but spends output tokens on reasoning, which empties
                    # `content` when max_tokens is tight. Enable only for the quality tier with an
                    # ample budget; otherwise disable so caller budgets apply to the answer.
                    if self._think_mode == "on":
                        think = True
                    elif self._think_mode == "off":
                        think = False
                    else:  # "auto": quality tier (v4-pro, non-cheap) + room for reasoning
                        think = (not self._cheap) and max_tokens >= _THINK_MIN_TOKENS
                    if think:
                        # max_tokens caps reasoning + answer together, so reasoning would eat into the
                        # answer and truncate it. Give reasoning ADDITIVE headroom so the answer keeps
                        # its full caller-requested budget.
                        ds_params["max_tokens"] = max_tokens + _REASONING_HEADROOM
                    else:
                        ds_params["extra_body"] = {"thinking": {"type": "disabled"}}
                    response = await self._client.chat.completions.create(**ds_params)
                    usage = response.usage
                    out_tok = usage.completion_tokens
                    cache_read_tok = getattr(usage, "prompt_cache_hit_tokens", 0) or 0
                    miss_tok = getattr(usage, "prompt_cache_miss_tokens", None)
                    if miss_tok is None:
                        miss_tok = max((usage.prompt_tokens or 0) - cache_read_tok, 0)
                    in_tok = miss_tok
                    cost = (
                        miss_tok * rates["input"]
                        + cache_read_tok * rates["cache_read"]
                        + out_tok * rates["output"]
                    ) / 1_000_000
                    text = response.choices[0].message.content
                else:
                    # Anthropic. Cache the system prompt (large, rarely changes) for ~90% savings.
                    system_param: list | str = (
                        [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
                        if use_cache else system
                    )
                    # Opus 4.7+ removed temperature/top_p/top_k — passing them returns 400
                    # Opus 4.7+ and the Claude 5 family (opus-4-8, sonnet-5, fable-5) removed
                    # temperature/top_p/top_k — passing them returns 400.
                    _no_sampling = any(m in self._model for m in
                                       ("opus-4-7", "opus-4-8", "sonnet-5", "fable-5"))
                    params: dict = dict(
                        model=self._model, max_tokens=max_tokens,
                        system=system_param, messages=messages,
                    )
                    if not _no_sampling:
                        params["temperature"] = temperature
                    # Sonnet 5 defaults thinking ON (Opus 4.8 defaults it off); with thinking on,
                    # reasoning eats max_tokens and can empty `content`. These are deterministic
                    # generation tasks — disable thinking so the budget applies to the answer.
                    # Accepted on opus-4-8 / opus-4-7 / sonnet-5 (fable-5 would 400, but we don't use it).
                    if any(m in self._model for m in ("opus-4-8", "opus-4-7", "sonnet-5")):
                        params["thinking"] = {"type": "disabled"}
                    response = await self._client.messages.create(**params)
                    usage = response.usage
                    in_tok = usage.input_tokens
                    out_tok = usage.output_tokens
                    cache_read_tok = getattr(usage, "cache_read_input_tokens", 0)
                    cost = (
                        in_tok * rates["input"]
                        + out_tok * rates["output"]
                        + getattr(usage, "cache_creation_input_tokens", 0) * rates["cache_write"]
                        + cache_read_tok * rates["cache_read"]
                    ) / 1_000_000
                    # Extended/interleaved thinking puts a ThinkingBlock first, so content[0] may
                    # not be text — concatenate all TEXT blocks (skip thinking/tool blocks).
                    text = "".join(getattr(b, "text", "") for b in response.content
                                   if getattr(b, "type", None) == "text")

                elapsed = time.monotonic() - t0
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
                            "in": in_tok, "out": out_tok,
                            "cache_read": cache_read_tok, "cost": round(cost, 6),
                        }) + "\n")
                except Exception:
                    pass

                logger.log_decision(
                    action="llm_call",
                    outcome="success",
                    details={
                        "agent": self._agent,
                        "model": self._model,
                        "input_tokens": in_tok,
                        "output_tokens": out_tok,
                        "cache_read_tokens": cache_read_tok,
                        "cost_usd": round(cost, 6),
                        "latency_s": round(elapsed, 2),
                    },
                )

                if text is None:
                    raise RuntimeError("LLM returned empty content")
                _note_auth_success()          # key is alive — clear any 401 streak
                return text

            except (anthropic.RateLimitError, openai.RateLimitError) as e:
                wait = 2 ** attempt * 5  # 5s, 10s, 20s
                logger.warning("llm_rate_limit", attempt=attempt + 1, wait_s=wait)
                await asyncio.sleep(wait)
                last_error = e

            except (anthropic.APIError, openai.APIError) as e:
                logger.error("llm_api_error", attempt=attempt + 1, error=str(e))
                if _is_auth_error(e):         # dead/rotated key → watchdog (alerts past threshold)
                    _note_auth_failure(self._agent, self._model, str(e))
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
