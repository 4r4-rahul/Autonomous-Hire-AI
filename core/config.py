from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # LLM provider routing: "anthropic" or "deepseek". DeepSeek is OpenAI-compatible and far
    # cheaper; kept as a toggle so tailoring can flip back to Claude the moment it's preferred.
    llm_provider: str = Field(default="anthropic", alias="LLM_PROVIDER")
    # Optional per-tier override: when set (e.g. "anthropic"), the QUALITY tier (résumé tailoring,
    # cover letters, interview prep, the quality-fix rewrite — anything NOT the cheap haiku tier)
    # uses this provider while the cheap high-volume tier follows LLM_PROVIDER. Empty = same for all.
    llm_quality_provider: str = Field(default="", alias="LLM_QUALITY_PROVIDER")

    # Anthropic
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_model: str = "claude-opus-4-8"

    # DeepSeek (OpenAI-compatible, https://api.deepseek.com). v4-pro = quality path (tailoring),
    # v4-flash = cheap high-volume path (scoring/classification/intake).
    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(default="https://api.deepseek.com", alias="DEEPSEEK_BASE_URL")
    deepseek_model_pro: str = Field(default="deepseek-v4-pro", alias="DEEPSEEK_MODEL_PRO")
    deepseek_model_flash: str = Field(default="deepseek-v4-flash", alias="DEEPSEEK_MODEL_FLASH")
    # v4 models "think" (emit reasoning) by default. Reasoning improves quality but burns output
    # tokens/latency and empties `content` when max_tokens is tight. Mode:
    #   "auto" (default) — think on the QUALITY tier (v4-pro: tailoring/cover/prep), NOT the cheap
    #                      high-volume tier (v4-flash), and only when the token budget has room.
    #   "on"            — always think.   "off" — never think.
    deepseek_thinking: str = Field(default="auto", alias="DEEPSEEK_THINKING")

    # Pipeline mode — the reversible full-time PAUSE. "fulltime" = normal W2 discovery+apply;
    # "contract" = C2C/C2H ONLY: pauses full-time discovery (product-co boards, LinkedIn full-time),
    # runs every vendor/staffing/consulting/implementation-partner scanner, and the intake gate rejects
    # any role that isn't contract. Flip back to "fulltime" to resume — nothing is deleted.
    pipeline_mode: str = Field(default="fulltime", alias="PIPELINE_MODE")

    # Hands-off auto-apply (the IRREVERSIBLE auto-submit lane). Routed through Settings — which loads
    # .env — because auto_submit_enabled() previously read os.getenv directly, and nothing populates
    # os.environ from .env, so the .env opt-in was a SILENT NO-OP (looked enabled, never fired).
    # Default OFF: a new irreversible sender must be explicitly turned on. Cap is per CALENDAR DAY.
    auto_submit_enabled: bool = Field(default=False, alias="AUTONOMOUSHIRE_AUTO_SUBMIT")
    auto_submit_max_per_day: int = Field(default=5, alias="AUTO_SUBMIT_MAX_PER_DAY")

    # Google
    gmail_client_id: str = Field(default="", alias="GMAIL_CLIENT_ID")
    gmail_client_secret: str = Field(default="", alias="GMAIL_CLIENT_SECRET")
    gmail_refresh_token: str = Field(default="", alias="GMAIL_REFRESH_TOKEN")
    google_calendar_id: str = Field(default="primary", alias="GOOGLE_CALENDAR_ID")

    # Workday (shared account across *.myworkdayjobs.com) — secret, lives in .env not the profile YAML
    workday_password: str = Field(default="", alias="WORKDAY_PASSWORD")

    # LinkedIn
    linkedin_li_at: str = Field(default="", alias="LINKEDIN_LI_AT")
    linkedin_jsessionid: str = Field(default="", alias="LINKEDIN_JSESSIONID")
    linkedin_profile_dir: str = Field(default="./data/chrome_profile", alias="LI_PROFILE_DIR")
    linkedin_pause: bool = Field(default=False, alias="LI_PAUSE")  # kill switch
    linkedin_active_hours_start: int = Field(default=8, alias="LI_ACTIVE_HOURS_START")
    linkedin_active_hours_end: int = Field(default=21, alias="LI_ACTIVE_HOURS_END")

    # Discord (review queue + daily digest)
    discord_webhook_review: str = Field(default="", alias="DISCORD_WEBHOOK_REVIEW")
    discord_webhook_digest: str = Field(default="", alias="DISCORD_WEBHOOK_DIGEST")

    # Human-in-the-loop gates
    hitl_new_ats: bool = Field(default=True, alias="HUMAN_IN_LOOP_NEW_ATS")
    hitl_low_confidence: bool = Field(default=True, alias="HUMAN_IN_LOOP_LOW_CONF")
    hitl_high_value_contact: bool = Field(default=True, alias="HUMAN_IN_LOOP_HIGH_VALUE")

    # Search
    tavily_api_key: str = Field(default="", alias="TAVILY_API_KEY")

    # Vector DB
    chroma_persist_dir: str = Field(default="./data/chroma_db", alias="CHROMA_PERSIST_DIR")
    pinecone_api_key: str = Field(default="", alias="PINECONE_API_KEY")
    pinecone_index_name: str = Field(default="autonomoushire", alias="PINECONE_INDEX_NAME")

    # Redis
    redis_url: str = Field(default="redis://localhost:6379", alias="REDIS_URL")

    # App
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    environment: str = Field(default="development", alias="ENVIRONMENT")
    dashboard_port: int = Field(default=8501, alias="DASHBOARD_PORT")
    approval_webhook_url: str = Field(default="", alias="APPROVAL_WEBHOOK_URL")

    # Thresholds (can be overridden via env)
    min_fit_score: int = Field(default=65, alias="MIN_FIT_SCORE")
    min_app_quality: int = Field(default=70, alias="MIN_APP_QUALITY")
    min_quantity_per_week: int = Field(default=10, alias="MIN_QUANTITY_PER_WEEK")
    min_response_rate: float = Field(default=0.15, alias="MIN_RESPONSE_RATE")
    apply_max_per_day: int = Field(default=15, alias="APPLY_MAX_PER_DAY")
    outreach_max_per_day: int = Field(default=3, alias="OUTREACH_MAX_PER_DAY")

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
