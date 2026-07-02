"""
LLM client — single abstraction over the model provider.
Supports Groq (hosted, for the public deployment) or a local Ollama server
(unlimited, free, for fast local development) via LLM_PROVIDER in config.
Both are OpenAI-compatible endpoints, so the same SDK/request shape works for either.
"""
import json
import logging
import time
import threading
from collections import deque
from typing import Optional, Union
from groq import Groq
from openai import OpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core import config

logger = logging.getLogger(__name__)

_client: Optional[Union[Groq, OpenAI]] = None

# Rate limiter — Groq free tier is 30 RPM for Llama 3.3 70B
_RATE_LIMIT_RPM = 25
_request_timestamps: deque = deque()
_rate_lock = threading.Lock()


def _throttle():
    """Block if we'd exceed the per-minute rate limit."""
    with _rate_lock:
        now = time.time()
        while _request_timestamps and now - _request_timestamps[0] > 60:
            _request_timestamps.popleft()

        if len(_request_timestamps) >= _RATE_LIMIT_RPM:
            wait_time = 60 - (now - _request_timestamps[0]) + 0.5
            if wait_time > 0:
                logger.info("Rate limit: sleeping %.1fs to stay under %d RPM",
                           wait_time, _RATE_LIMIT_RPM)
                time.sleep(wait_time)
                now = time.time()
                while _request_timestamps and now - _request_timestamps[0] > 60:
                    _request_timestamps.popleft()

        _request_timestamps.append(now)


def get_client() -> Union[Groq, OpenAI]:
    global _client
    if _client is None:
        if config.LLM_PROVIDER == "ollama":
            # The Groq SDK hardcodes its own request path (/openai/v1/chat/completions)
            # regardless of base_url, so it can't talk to Ollama's actual OpenAI-
            # compatible path (/v1/chat/completions). Use the real openai SDK instead —
            # Ollama ignores the API key but the SDK requires a non-empty string.
            _client = OpenAI(api_key="ollama", base_url=config.OLLAMA_BASE_URL)
        else:
            if not config.GROQ_API_KEY:
                raise RuntimeError(
                    "GROQ_API_KEY not set. Get a free key at https://console.groq.com"
                )
            _client = Groq(api_key=config.GROQ_API_KEY)
    return _client


def _active_model() -> str:
    return config.OLLAMA_MODEL if config.LLM_PROVIDER == "ollama" else config.LLM_MODEL


@retry(
    stop=stop_after_attempt(config.LLM_MAX_RETRIES + 1),
    wait=wait_exponential(multiplier=2, min=2, max=20),
    reraise=True,
)
def llm_complete(
    system: str,
    user: str,
    json_mode: bool = False,
    temperature: Optional[float] = None,
) -> str:
    if config.LLM_PROVIDER != "ollama":
        _throttle()  # Ollama is local — no daily/per-minute quota to protect
    try:
        client = get_client()
        kwargs = {
            "model": _active_model(),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature if temperature is not None else config.LLM_TEMPERATURE,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as e:
            err_str = str(e).lower()
            if json_mode and "response_format" in err_str:
                logger.warning("Model doesn't support JSON mode, falling back")
                kwargs.pop("response_format", None)
                resp = client.chat.completions.create(**kwargs)
            elif "rate_limit" in err_str or "429" in err_str:
                import re
                m = re.search(r"try again in ([\d.]+)s", str(e))
                wait = float(m.group(1)) + 1 if m else 10
                logger.warning("Rate limit hit, sleeping %.1fs", wait)
                time.sleep(wait)
                raise
            else:
                raise
        return resp.choices[0].message.content or ""
    except Exception as e:
        logger.error("LLM call failed (%s): %s", type(e).__name__, str(e)[:200])
        raise


def llm_json(system: str, user: str, temperature: Optional[float] = None) -> dict | list:
    raw = llm_complete(system, user, json_mode=True, temperature=temperature)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        cleaned = raw.strip().strip("`").replace("json\n", "", 1)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning("LLM returned non-JSON, returning empty dict. Raw: %s", raw[:200])
            return {}