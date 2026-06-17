from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM Provider (OpenAI-compatible)
    llm_base_url: str = "https://your-llm-provider/v1"
    llm_api_key: str = ""
    orchestrator_model: str = "minimax/minimax-m2.5"
    reranker_model: str = "qwen/qwen3-reranker-8b"
    generator_model: str = "qwen/qwen3-5-27b"

    # Jira Server (read + create only)
    jira_server_url: str = "http://localhost:8080"
    jira_user_email: str = ""
    jira_api_token: str = ""
    use_mock_jira: bool = True              # False → call real Jira API
    use_mock_confluence: bool = False       # True → return sample PRD instead of fetching
    jira_set_reporter_from_teams: bool = False  # When True + bot PAT mode: set reporter to Teams user (fallback: bot username)
    jira_epic_link_field: str = "customfield_10101"    # varies per Jira instance
    jira_epic_name_field: str = "customfield_10103"    # varies per Jira instance
    jira_story_points_field: str = "customfield_10801" # varies per Jira instance
    jira_sprint_field: str = "customfield_10007"       # varies per instance, auto-synced

    # Confluence Server (read-only)
    confluence_server_url: str = "http://localhost:8090"
    confluence_user_email: str = ""
    confluence_api_token: str = ""

    # Teams Bot — blank for local/Emulator, fill only for Azure/Teams deploy
    teams_bot_app_id: str = ""
    teams_bot_app_password: str = ""
    teams_bot_tenant_id: str = ""  # Tenant ID for single-tenant Azure AD apps

    # Host header overrides (legacy, kept for compatibility — prefer DNS_OVERRIDES)
    jira_host_header: str = ""
    confluence_host_header: str = ""

    # DNS overrides — route specific hostnames to IPs when platform DNS can't resolve them.
    # Format: "hostname1:ip1,hostname2:ip2"
    # Example: "jira.zalopay.vn:49.213.117.10,confluence.zalopay.vn:49.213.117.10"
    # Hostname and SNI stay unchanged; only the TCP connection target is overridden.
    dns_overrides: str = ""
    teams_domain: str = "vng.com.vn"             # org email domain for user mapping
    teams_test_user_email: str = ""              # override identity in Emulator (local dev only)
    # Set EMULATOR_MODE=true when testing locally with Bot Framework Emulator.
    # Disables JWT auth so the Emulator can connect without credentials even when
    # TEAMS_BOT_APP_ID is set (e.g. you have Azure credentials in .env but need
    # local testing). NEVER set this in production.
    emulator_mode: bool = False

    # App
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"
    debug: bool = False

    # Surface toggles (secure-by-default for production)
    # Local/Emulator: set ENABLE_MESSAGES_ENDPOINT=true. Production with Azure Bot
    # Service: leave false — the local /api/messages bot is not exposed.
    enable_messages_endpoint: bool = False
    # Diagnostic endpoints (/api/jira/check, /api/confluence/check) — info disclosure
    # + SSRF surface. Keep OFF in production.
    enable_diagnostics: bool = False
    # Swagger/OpenAPI docs — keep OFF in production.
    enable_docs: bool = False

    # AgentBase Memory & Identity
    memory_id: str = ""
    agent_identity_name: str = "clawathon-agent"

    # Rate limiting (applied to the bot message handler)
    rate_limit_enabled: bool = True
    rate_limit_max_requests: int = 120              # max requests per window
    rate_limit_window_seconds: int = 60             # window length in seconds
    rate_limit_max_queue_wait_seconds: int = 300    # reject if queue wait exceeds this


settings = Settings()
