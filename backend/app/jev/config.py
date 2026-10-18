"""Configuration for Jev classification."""

import os
from dataclasses import dataclass


@dataclass
class JevConfig:
    """Jev classification configuration."""

    api_key: str
    api_endpoint: str = "https://openrouter.ai/api/alpha/decisions"
    model: str = "typesafe/jev-1.13.0"
    enabled: bool = False
    confidence_threshold: float = 0.70
    timeout_seconds: int = 10
    max_retries: int = 3

    @classmethod
    def from_env(cls) -> "JevConfig":
        """Load configuration from environment variables."""
        api_key = os.environ.get("JEV_API_KEY", "")
        if not api_key:
            raise ValueError(
                "JEV_API_KEY environment variable is required. "
                "Get an API key from OpenRouter: https://openrouter.ai/keys"
            )

        return cls(
            api_key=api_key,
            api_endpoint=os.environ.get(
                "JEV_API_ENDPOINT", "https://openrouter.ai/api/alpha/decisions"
            ),
            model=os.environ.get("JEV_MODEL", "typesafe/jev-1.13.0"),
            enabled=os.environ.get("JEV_ENABLED", "false").lower() == "true",
            confidence_threshold=float(os.environ.get("JEV_CONFIDENCE_THRESHOLD", "0.70")),
            timeout_seconds=int(os.environ.get("JEV_TIMEOUT_SECONDS", "10")),
            max_retries=int(os.environ.get("JEV_MAX_RETRIES", "3")),
        )
