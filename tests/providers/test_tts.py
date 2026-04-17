"""Tests for the TTS provider layer."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.providers.tts import OpenAITTSProvider


def test_unsupported_format_raises():
    with pytest.raises(ValueError):
        OpenAITTSProvider(api_key="k", audio_format="wma")


async def test_synthesize_requires_api_key(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = OpenAITTSProvider(api_key="")
    with pytest.raises(RuntimeError, match="API key missing"):
        await provider.synthesize(
            text="hoi", voice="nova", output_path=tmp_path / "x.opus"
        )


async def test_synthesize_rejects_empty_text(tmp_path: Path):
    provider = OpenAITTSProvider(api_key="k")
    with pytest.raises(ValueError, match="empty"):
        await provider.synthesize(
            text="   ", voice="nova", output_path=tmp_path / "x.opus"
        )


async def test_synthesize_streams_to_file(tmp_path: Path):
    provider = OpenAITTSProvider(api_key="k")
    output = tmp_path / "sub" / "out.opus"

    fake_response = MagicMock()
    fake_response.stream_to_file = AsyncMock()
    fake_stream_ctx = MagicMock()
    fake_stream_ctx.__aenter__ = AsyncMock(return_value=fake_response)
    fake_stream_ctx.__aexit__ = AsyncMock(return_value=False)

    fake_client = MagicMock()
    fake_client.audio.speech.with_streaming_response.create = MagicMock(
        return_value=fake_stream_ctx
    )

    with patch("openai.AsyncOpenAI", return_value=fake_client):
        result = await provider.synthesize(
            text="hallo daar",
            voice="nova",
            output_path=output,
            instructions="warm",
        )

    assert result == output
    assert output.parent.exists()
    fake_client.audio.speech.with_streaming_response.create.assert_called_once()
    kwargs = fake_client.audio.speech.with_streaming_response.create.call_args.kwargs
    assert kwargs["voice"] == "nova"
    assert kwargs["input"] == "hallo daar"
    assert kwargs["instructions"] == "warm"
    assert kwargs["response_format"] == "opus"
