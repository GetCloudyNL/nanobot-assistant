"""Text-to-speech providers for generating audio from text."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path

from loguru import logger


class TTSProvider(ABC):
    """Abstract base for text-to-speech providers."""

    @abstractmethod
    async def synthesize(
        self,
        text: str,
        voice: str,
        output_path: Path,
        instructions: str | None = None,
    ) -> Path:
        """Synthesize *text* to an audio file at *output_path*.

        ``voice`` identifies a provider-specific voice.
        ``instructions`` is an optional style hint the provider may honour.
        Returns the path to the generated audio file.
        """


class OpenAITTSProvider(TTSProvider):
    """OpenAI text-to-speech via gpt-4o-mini-tts (or compatible).

    Multilingual (including Dutch), supports style instructions and
    several preset voices (alloy, echo, fable, nova, onyx, shimmer, ...).
    """

    DEFAULT_MODEL = "gpt-4o-mini-tts"
    SUPPORTED_FORMATS = {"mp3", "opus", "wav", "flac", "aac", "pcm"}

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        audio_format: str = "opus",
        api_base: str | None = None,
    ):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model or self.DEFAULT_MODEL
        if audio_format not in self.SUPPORTED_FORMATS:
            raise ValueError(
                f"Unsupported TTS format {audio_format!r}. "
                f"Choose one of {sorted(self.SUPPORTED_FORMATS)}."
            )
        self.audio_format = audio_format
        self.api_base = api_base

    async def synthesize(
        self,
        text: str,
        voice: str,
        output_path: Path,
        instructions: str | None = None,
    ) -> Path:
        if not self.api_key:
            raise RuntimeError(
                "OpenAI API key missing; set providers.openai.apiKey in config "
                "or OPENAI_API_KEY in environment."
            )
        if not text.strip():
            raise ValueError("TTS text is empty.")

        from openai import AsyncOpenAI

        client_kwargs: dict = {"api_key": self.api_key}
        if self.api_base:
            client_kwargs["base_url"] = self.api_base
        client = AsyncOpenAI(**client_kwargs)

        request_kwargs: dict = {
            "model": self.model,
            "voice": voice,
            "input": text,
            "response_format": self.audio_format,
        }
        if instructions:
            request_kwargs["instructions"] = instructions

        logger.debug(
            "OpenAI TTS: voice={} model={} format={} chars={}",
            voice, self.model, self.audio_format, len(text),
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)

        async with client.audio.speech.with_streaming_response.create(
            **request_kwargs,
        ) as response:
            await response.stream_to_file(output_path)

        return output_path
