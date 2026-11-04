"""Engine settings, read from the environment (and `.env` at the repo root, if present)."""
from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path = REPO_ROOT / ".env") -> None:
    """Minimal KEY=VALUE loader. Existing environment variables win."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value


def _cap(raw: str | None, default: int | None) -> int | None:
    if raw is None or raw == "":
        return default
    return None if raw in ("0", "inf") else int(raw)


class Settings(BaseModel):
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    realtime_model: str = "gpt-realtime-2.1"
    realtime_model_mini: str = "gpt-realtime-2.1-mini"
    default_model: str = "mini"  # "mini" | "flagship": what a request without a model gets (decided 4 Nov)
    voice: str = "marin"
    # Transcripts are for the screen and the log only; the model reasons on audio.
    # gpt-4o-mini-transcribe (the SDK default) is deprecated, shutdown 2027-02-26.
    transcription_model: str = "gpt-transcribe"
    reasoning_effort: str = "low"
    client_secret_ttl_s: int = 120
    secret_pool: bool = True  # keep one pre-minted secret per session config, so connecting skips the mint
    pool_secret_ttl_s: int = 600  # pooled secrets wait in engine memory; refreshed before they expire
    # see engine.api.realtime.TURN_MODES. Push-to-talk decided 4 Nov (eval/REPORT-2026-11-04.md);
    # semantic VAD stays switchable from the page. The talk key (default Space) is a page setting.
    turn_mode: str = "ptt"
    # see engine.session.prompt.PROMPTS. v3_local (8 tools, local clip) since the Phase 2 spoken
    # runs (eval/REPORT-2026-11-06.md); v2_local knows only get_reading.
    prompt: str = "v3_local"
    # Safety net only: about 8 s of speech. Measured 4 Nov: 20 audio tokens per second of reply,
    # plus transcript text tokens (0.3-0.8x the audio tokens); output_tokens = audio + text.
    # None = no cap ("inf").
    max_output_tokens: int | None = 220
    dummy_delay_s: float = 1.5  # fake bench meter read time (kept from Phase 1 for comparable latency runs)
    vision_timeout_s: float = 4.0  # decided setting: hard timeout for a camera read
    fake_scenario: str = "short_battery_line"
    fake_time_scale: float = 1.0  # fake bench delays (supply read, meter read, boot watch); tests use 0
    dev_endpoints: bool = True  # GET /dev/sessions/{id} (bound to 127.0.0.1 with the rest of the engine)
    tool_timeout_s: float = 6.0
    host: str = "127.0.0.1"
    port: int = 8000
    db_path: Path = REPO_ROOT / "data" / "benchmate.sqlite"
    web_dist: Path = REPO_ROOT / "apps" / "web" / "dist"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        env = os.environ
        defaults = cls()
        db = Path(env.get("BENCHMATE_DB", defaults.db_path))
        return cls(
            openai_api_key=env.get("OPENAI_API_KEY") or None,
            openai_base_url=env.get("OPENAI_BASE_URL", defaults.openai_base_url),
            realtime_model=env.get("BENCHMATE_REALTIME_MODEL", defaults.realtime_model),
            realtime_model_mini=env.get("BENCHMATE_REALTIME_MODEL_MINI", defaults.realtime_model_mini),
            default_model=env.get("BENCHMATE_DEFAULT_MODEL", defaults.default_model),
            voice=env.get("BENCHMATE_VOICE", defaults.voice),
            transcription_model=env.get("BENCHMATE_TRANSCRIPTION_MODEL", defaults.transcription_model),
            reasoning_effort=env.get("BENCHMATE_REASONING_EFFORT", defaults.reasoning_effort),
            client_secret_ttl_s=int(env.get("BENCHMATE_CLIENT_SECRET_TTL_S", defaults.client_secret_ttl_s)),
            secret_pool=env.get("BENCHMATE_SECRET_POOL", "1") != "0",
            pool_secret_ttl_s=int(env.get("BENCHMATE_POOL_SECRET_TTL_S", defaults.pool_secret_ttl_s)),
            turn_mode=env.get("BENCHMATE_TURN_MODE", defaults.turn_mode),
            prompt=env.get("BENCHMATE_PROMPT", defaults.prompt),
            max_output_tokens=_cap(env.get("BENCHMATE_MAX_OUTPUT_TOKENS"), defaults.max_output_tokens),
            dummy_delay_s=float(env.get("BENCHMATE_DUMMY_DELAY_S", defaults.dummy_delay_s)),
            vision_timeout_s=float(env.get("BENCHMATE_VISION_TIMEOUT_S", defaults.vision_timeout_s)),
            fake_scenario=env.get("BENCHMATE_FAKE_SCENARIO", defaults.fake_scenario),
            fake_time_scale=float(env.get("BENCHMATE_FAKE_TIME_SCALE", defaults.fake_time_scale)),
            dev_endpoints=env.get("BENCHMATE_DEV_ENDPOINTS", "1") != "0",
            host=env.get("BENCHMATE_HOST", defaults.host),
            port=int(env.get("BENCHMATE_PORT", defaults.port)),
            db_path=db if db.is_absolute() else REPO_ROOT / db,
        )

    def resolve_model(self, requested: str | None) -> str | None:
        """Map "flagship"/"mini" (or an exact allowed name) to a model id. None if not allowed."""
        aliases = {
            None: self.realtime_model_mini if self.default_model == "mini" else self.realtime_model,
            "flagship": self.realtime_model,
            "mini": self.realtime_model_mini,
            self.realtime_model: self.realtime_model,
            self.realtime_model_mini: self.realtime_model_mini,
        }
        return aliases.get(requested)
