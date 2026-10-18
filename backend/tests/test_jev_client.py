"""Tests for Jev API client (unit tests without real API calls)."""

import pytest
from unittest.mock import AsyncMock, Mock, patch

from app.jev.client import JevApiError, JevClient, JevResponse
from app.jev.config import JevConfig


@pytest.fixture
def config():
    """Jev config for testing."""
    return JevConfig(
        api_key="test_api_key",
        api_endpoint="https://test.example.com/decisions",
        model="typesafe/jev-1.13.0",
        timeout_seconds=5,
        max_retries=3,
    )


@pytest.fixture
def client(config):
    """Jev client instance."""
    return JevClient(config)


@pytest.mark.asyncio
async def test_successful_classification(client):
    """Test successful Jev API call."""
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.text = """
    {
        "model": "typesafe/jev-1.13.0",
        "answers": {
            "criticality": {
                "type": "choice",
                "choice": "high",
                "confidence": 0.89,
                "probabilities": {
                    "critical": 0.08,
                    "high": 0.74,
                    "medium": 0.16,
                    "low": 0.02
                }
            }
        },
        "usage": {
            "input_tokens": 248,
            "output_tokens": 0,
            "cost": 0.000010416
        }
    }
    """

    with patch.object(client.client, "post", AsyncMock(return_value=mock_response)):
        state = {"alert_context": "Test alert"}
        questions = {"criticality": {"type": "choice"}}

        result = await client.classify(state, questions)

        assert isinstance(result, JevResponse)
        assert result.model == "typesafe/jev-1.13.0"
        assert result.answers["criticality"].choice == "high"
        assert result.answers["criticality"].confidence == 0.89
        assert result.usage.cost == 0.000010416


@pytest.mark.asyncio
async def test_retryable_error_429(client):
    """Test retry on 429 rate limit error."""
    mock_response_429 = Mock()
    mock_response_429.status_code = 429
    mock_response_429.text = "Rate limit exceeded"
    mock_response_429.headers = {"Retry-After": "0.1"}

    mock_response_200 = Mock()
    mock_response_200.status_code = 200
    mock_response_200.text = """
    {
        "model": "typesafe/jev-1.13.0",
        "answers": {
            "criticality": {
                "type": "choice",
                "choice": "medium"
            }
        },
        "usage": {
            "input_tokens": 100,
            "output_tokens": 0,
            "cost": 0.0000042
        }
    }
    """

    with patch.object(
        client.client, "post", AsyncMock(side_effect=[mock_response_429, mock_response_200])
    ):
        state = {"alert_context": "Test"}
        questions = {"criticality": {"type": "choice"}}

        result = await client.classify(state, questions)

        assert result.answers["criticality"].choice == "medium"


@pytest.mark.asyncio
async def test_non_retryable_error_400(client):
    """Test non-retryable error (400 Bad Request)."""
    mock_response = Mock()
    mock_response.status_code = 400
    mock_response.text = "Invalid request"

    with patch.object(client.client, "post", AsyncMock(return_value=mock_response)):
        state = {"alert_context": "Test"}
        questions = {"criticality": {"type": "choice"}}

        with pytest.raises(JevApiError) as exc_info:
            await client.classify(state, questions)

        assert exc_info.value.status_code == 400
        assert exc_info.value.retryable is False


@pytest.mark.asyncio
async def test_max_retries_exceeded(client):
    """Test that max retries is respected."""
    mock_response = Mock()
    mock_response.status_code = 500
    mock_response.text = "Internal server error"
    mock_response.headers = {}

    with patch.object(client.client, "post", AsyncMock(return_value=mock_response)):
        state = {"alert_context": "Test"}
        questions = {"criticality": {"type": "choice"}}

        with pytest.raises(JevApiError) as exc_info:
            await client.classify(state, questions)

        # Should have tried 3 times (max_retries)
        assert client.client.post.call_count == 3


@pytest.mark.asyncio
async def test_in_flight_budget_retryable(client):
    """Test that in-flight budget 402 error is retryable."""
    mock_response_402 = Mock()
    mock_response_402.status_code = 402
    mock_response_402.text = '{"error": {"metadata": {"limit_source": "openrouter_in_flight_budget"}}}'
    mock_response_402.headers = {}

    mock_response_200 = Mock()
    mock_response_200.status_code = 200
    mock_response_200.text = """
    {
        "model": "typesafe/jev-1.13.0",
        "answers": {"criticality": {"type": "choice", "choice": "low"}},
        "usage": {"input_tokens": 50, "output_tokens": 0, "cost": 0.0000021}
    }
    """

    with patch.object(
        client.client, "post", AsyncMock(side_effect=[mock_response_402, mock_response_200])
    ):
        state = {"alert_context": "Test"}
        questions = {"criticality": {"type": "choice"}}

        result = await client.classify(state, questions)

        assert result.answers["criticality"].choice == "low"


@pytest.mark.asyncio
async def test_other_402_not_retryable(client):
    """Test that other 402 errors are not retryable."""
    mock_response = Mock()
    mock_response.status_code = 402
    mock_response.text = '{"error": {"message": "Insufficient credits"}}'

    with patch.object(client.client, "post", AsyncMock(return_value=mock_response)):
        state = {"alert_context": "Test"}
        questions = {"criticality": {"type": "choice"}}

        with pytest.raises(JevApiError) as exc_info:
            await client.classify(state, questions)

        assert exc_info.value.status_code == 402
        assert exc_info.value.retryable is False
