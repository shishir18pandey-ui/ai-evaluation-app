"""
LLM client — single abstraction over the model provider.
Uses Groq (free, fast, supports Llama 3.3 70B open-weight model).
"""
import json
import logging
import time
import threading
from collections import deque
from typing import Optional
from groq import Groq
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core import config

logger = logging.getLogger(__name__)

_client: Optional[Groq] = None

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


def get_client() -> Groq:
    global _client
    if _client is None:
        if not config.GROQ_API_KEY:
            raise RuntimeError(
                "GROQ_API_KEY not set. Get a free key at https://console.groq.com"
            )
        _client = Groq(api_key=config.GROQ_API_KEY)
    return _client


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
    _throttle()
    try:
        client = get_client()
        kwargs = {
            "model": config.LLM_MODEL,
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