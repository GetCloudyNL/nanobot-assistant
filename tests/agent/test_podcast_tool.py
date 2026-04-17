"""Tests for the podcast tool: script parsing, dispatch, guards."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from nanobot.agent.tools.podcast import PodcastTool, _slugify
from nanobot.config.schema import PodcastHostConfig, PodcastToolConfig
from nanobot.providers.base import LLMResponse


class _FakeProvider:
    """Minimal async chat stub returning a preset response."""

    def __init__(self, reply: str):
        self._reply = reply
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return LLMResponse(content=self._reply, finish_reason="stop")


def _make_tool(
    tmp_path: Path,
    *,
    enabled: bool = True,
    reply: str = "[]",
    tts: object | None = None,
) -> tuple[PodcastTool, _FakeProvider]:
    cfg = PodcastToolConfig(
        enabled=enabled,
        hosts=[
            PodcastHostConfig(name="Sanne", voice="nova", style="warm"),
            PodcastHostConfig(name="Daan", voice="onyx", style="calm"),
        ],
    )
    provider = _FakeProvider(reply)
    tool = PodcastTool(
        workspace=tmp_path,
        config=cfg,
        provider=provider,
        model="test-model",
        tts_provider=tts,
    )
    return tool, provider


async def test_disabled_tool_returns_friendly_error(tmp_path: Path):
    tool, _ = _make_tool(tmp_path, enabled=False)
    result = await tool.execute(source="any text")
    assert "disabled" in result.lower()


async def test_missing_tts_provider_returns_error(tmp_path: Path):
    tool, _ = _make_tool(tmp_path, enabled=True, tts=None)
    result = await tool.execute(source="any text")
    assert "tts provider" in result.lower()


async def test_script_parses_json_with_code_fences(tmp_path: Path):
    reply = (
        "```json\n"
        + json.dumps([
            {"host": "Sanne", "text": "Welkom."},
            {"host": "Daan", "text": "Vandaag bespreken we iets."},
        ])
        + "\n```"
    )
    tool, _ = _make_tool(tmp_path, reply=reply, tts=object())
    script = await tool._generate_script(
        content="Source text.",
        language="nl",
        style_hint=None,
        title="Test",
    )
    assert len(script) == 2
    assert script[0]["host"] == "Sanne"
    assert script[1]["text"].startswith("Vandaag")


async def test_script_salvages_when_wrapped_in_prose(tmp_path: Path):
    reply = "Here is the script:\n" + json.dumps(
        [{"host": "Sanne", "text": "Hi"}, {"host": "Daan", "text": "Hoi"}]
    )
    tool, _ = _make_tool(tmp_path, reply=reply, tts=object())
    script = await tool._generate_script(
        content="x", language="nl", style_hint=None, title=""
    )
    assert len(script) == 2


async def test_unknown_host_is_remapped(tmp_path: Path):
    reply = json.dumps(
        [
            {"host": "Ghost", "text": "Hallo"},
            {"host": "Sanne", "text": "Yo"},
        ]
    )
    tool, _ = _make_tool(tmp_path, reply=reply, tts=object())
    script = await tool._generate_script(
        content="x", language="nl", style_hint=None, title=""
    )
    assert {line["host"] for line in script}.issubset({"Sanne", "Daan"})


async def test_execute_without_ffmpeg_is_graceful(tmp_path: Path):
    reply = json.dumps(
        [
            {"host": "Sanne", "text": "Hi"},
            {"host": "Daan", "text": "Ho"},
        ]
    )
    tts = AsyncMock()
    tool, _ = _make_tool(tmp_path, reply=reply, tts=tts)
    with patch("nanobot.agent.tools.podcast.shutil.which", return_value=None):
        result = await tool.execute(source="Some text about things")
    assert "ffmpeg" in result.lower()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Hello World", "hello-world"),
        ("  multi   spaces !! ", "multi-spaces"),
        ("naïve Ælf", "na-ve-lf"),
        ("", ""),
    ],
)
def test_slugify(raw: str, expected: str):
    assert _slugify(raw) == expected
