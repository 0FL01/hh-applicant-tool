import logging
import time
from dataclasses import KW_ONLY, dataclass, field
from threading import Lock

import requests

from .base import AIError

logger = logging.getLogger(__package__)


DEFAULT_COMPLETION_ENDPOINT = "https://api.openai.com/v1/chat/completions"


class OpenAIError(AIError):
    pass


@dataclass
class ChatOpenAI:
    api_key: str
    _: KW_ONLY
    system_prompt: str | None = None
    timeout: float = 15.0
    temperature: float = 0.7
    max_completion_tokens: int = 1000
    model: str | None = None
    base_url: str = None
    session: requests.Session = field(default_factory=requests.Session)
    # --- NEW ---
    max_retries: int = 5
    rate_limit: int = 40
    _previous_request_time: float = field(default=0.0, init=False)
    _lock: Lock = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Normalize base_url: if it doesn't end with /chat/completions, append it
        if self.base_url:
            self.base_url = self.base_url.rstrip("/")
            if not self.base_url.endswith("/chat/completions"):
                self.base_url += "/chat/completions"
        else:
            self.base_url = DEFAULT_COMPLETION_ENDPOINT

    @property
    def _min_request_interval(self) -> float:
        """Minimum interval between requests based on rate limit (RPM)."""
        return 60.0 / self.rate_limit if self.rate_limit > 0 else 0.0

    def _request(self, payload: dict) -> requests.Response:
        """POST with rate limiting lock."""
        with self._lock:
            if self._previous_request_time > 0:
                delay = (
                    self._min_request_interval
                    - time.monotonic()
                    + self._previous_request_time
                )
                if delay > 0:
                    time.sleep(delay)
            try:
                return self.session.post(
                    self.base_url,
                    json=payload,
                    headers=self._default_headers(),
                    timeout=self.timeout,
                )
            finally:
                self._previous_request_time = time.monotonic()

    def _get_retry_delay(self, response: requests.Response, attempt: int) -> float:
        """Extract retry delay from Retry-After header, fall back to exponential backoff."""
        min_interval = self._min_request_interval or 1.0
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(float(retry_after), min_interval)
            except ValueError:
                try:
                    from email.utils import parsedate_to_datetime

                    return max(
                        parsedate_to_datetime(retry_after).timestamp() - time.time(),
                        min_interval,
                    )
                except Exception:
                    pass
        return max(min_interval * (attempt + 1), 1.0)

    def _default_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
        }

    def send_message(self, message: str) -> str:
        messages = []

        # Добавляем системный промпт только если он не пустой и не None
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})

        # Пользовательское сообщение всегда обязательно
        messages.append({"role": "user", "content": message})

        payload = {
            "messages": messages,
            "temperature": self.temperature,
            "max_completion_tokens": self.max_completion_tokens,
        }

        if self.model:
            payload["model"] = self.model

        for attempt in range(self.max_retries + 1):
            try:
                response = self._request(payload)
            except requests.exceptions.RequestException as ex:
                raise OpenAIError(f"Network error: {ex}") from ex

            if response.status_code == 429:
                if attempt >= self.max_retries:
                    raise OpenAIError("OpenAI rate limit exceeded")
                delay = self._get_retry_delay(response, attempt)
                logger.warning(
                    "Rate limited (attempt %d/%d), sleeping %.1fs",
                    attempt + 1,
                    self.max_retries,
                    delay,
                )
                time.sleep(delay)
                continue

            response.raise_for_status()

            data = response.json()
            if "error" in data:
                raise OpenAIError(data["error"]["message"])

            return data["choices"][0]["message"]["content"]
