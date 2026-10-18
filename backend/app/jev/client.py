"""Jev API client with retry logic."""

import asyncio
import json
import logging
from typing import Any

import httpx
from pydantic import BaseModel, Field

from .config import JevConfig

logger = logging.getLogger(__name__)


class JevChoiceAnswer(BaseModel):
    """Jev choice answer structure."""

    type: str = Field(..., pattern="^choice$")
    choice: str
    confidence: float | None = None
    probabilities: dict[str, float] = Field(default_factory=dict)


class JevUsage(BaseModel):
    """Jev usage statistics."""

    input_tokens: int
    output_tokens: int
    cost: float


class JevResponse(BaseModel):
    """Jev Decisions API response."""

    model: str
    answers: dict[str, JevChoiceAnswer]
    usage: JevUsage


class JevApiError(Exception):
    """Jev API error."""

    def __init__(self, status_code: int, message: str, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class JevClient:
    """
    HTTP client for Jev Decisions API.

    Handles retries with exponential backoff for transient errors.
    """

    def __init__(self, config: JevConfig):
        self.config = config
        self.client = httpx.AsyncClient(
            timeout=config.timeout_seconds,
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
        )

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def classify(
        self, state: dict[str, str], questions: dict[str, dict[str, Any]]
    ) -> JevResponse:
        """
        Call Jev Decisions API with retry logic.

        Args:
            state: State dictionary with context (e.g., {"alert_context": "..."})
            questions: Questions dictionary (e.g., {"criticality": {...}})

        Returns:
            JevResponse with answer and usage

        Raises:
            JevApiError: On non-retryable errors or after max retries
        """
        request_body = {
            "model": self.config.model,
            "state": state,
            "questions": questions,
        }

        for attempt in range(self.config.max_retries):
            try:
                response = await self.client.post(
                    self.config.api_endpoint,
                    json=request_body,
                )

                if response.status_code == 200:
                    return JevResponse.model_validate_json(response.text)

                # Check if error is retryable
                retryable = self._is_retryable_error(response.status_code, response.text)

                if not retryable:
                    raise JevApiError(
                        response.status_code,
                        f"Jev API error {response.status_code}: {response.text}",
                        retryable=False,
                    )

                # Retryable error
                retry_after = self._get_retry_delay(response.headers, attempt)
                logger.warning(
                    "Jev API retryable error %d on attempt %d/%d. Retrying after %.1fs",
                    response.status_code,
                    attempt + 1,
                    self.config.max_retries,
                    retry_after,
                )
                await asyncio.sleep(retry_after)

            except httpx.TimeoutException as exc:
                logger.warning(
                    "Jev API timeout on attempt %d/%d", attempt + 1, self.config.max_retries
                )
                if attempt + 1 >= self.config.max_retries:
                    raise JevApiError(0, f"Jev API timeout after {self.config.max_retries} retries") from exc
                await asyncio.sleep(self._exponential_backoff(attempt))

            except httpx.RequestError as exc:
                logger.warning(
                    "Jev API connection error on attempt %d/%d: %s",
                    attempt + 1,
                    self.config.max_retries,
                    exc,
                )
                if attempt + 1 >= self.config.max_retries:
                    raise JevApiError(
                        0, f"Jev API connection error after {self.config.max_retries} retries"
                    ) from exc
                await asyncio.sleep(self._exponential_backoff(attempt))

        raise JevApiError(0, f"Jev API failed after {self.config.max_retries} retries", retryable=False)

    def _is_retryable_error(self, status_code: int, body: str) -> bool:
        """Check if an error is retryable."""
        # 429 Rate limit - always retryable
        if status_code == 429:
            return True

        # 5xx Server errors - retryable
        if status_code >= 500:
            return True

        # 402 Payment Required - retryable only for in-flight budget limit
        if status_code == 402:
            try:
                error_data = json.loads(body)
                limit_source = error_data.get("error", {}).get("metadata", {}).get("limit_source")
                return limit_source == "openrouter_in_flight_budget"
            except (json.JSONDecodeError, KeyError):
                return False

        return False

    def _get_retry_delay(self, headers: httpx.Headers, attempt: int) -> float:
        """Get retry delay from Retry-After header or exponential backoff."""
        retry_after = headers.get("Retry-After")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass  # Ignore HTTP-date format

        return self._exponential_backoff(attempt)

    def _exponential_backoff(self, attempt: int) -> float:
        """Calculate exponential backoff with jitter."""
        import random

        base_delay = min(30.0, 1.0 * (2**attempt))
        jitter = random.uniform(0, 1.0)
        return base_delay + jitter
