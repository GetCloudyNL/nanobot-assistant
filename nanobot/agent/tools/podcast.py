"""Podcast tool: turn a URL or block of text into a short two-host audio podcast.

Flow:
1. Resolve content (fetch URL via web_fetch logic, or use given text).
2. Ask the LLM to write a Dutch/English dialogue script between two hosts.
3. Synthesize each line through the configured TTSProvider.
4. Concatenate all segments into a single audio file via ffmpeg.
5. Return a `[audio: <path>]` tag so the agent can pass it to the `message` tool.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.web import WebFetchTool

if TYPE_CHECKING:
    from nanobot.config.schema import PodcastToolConfig
    from nanobot.providers.base import LLMProvider
    from nanobot.providers.tts import TTSProvider


class PodcastTool(Tool):
    """Summarize a URL or text and render it as a two-host audio podcast."""

    name = "podcast"
    description = (
        "Create a short two-host audio podcast that summarizes a URL or block of text. "
        "Use this when the user asks to 'listen to', 'play as podcast', 'podcast'-ify, "
        "or otherwise wants an audio summary. Returns an [audio: /path] tag that MUST "
        "be forwarded via the `message` tool's `media` parameter to deliver it."
    )
    parameters = {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": (
                    "URL to summarize, OR a block of text to turn into a podcast. "
                    "If it starts with http(s):// it is fetched; otherwise treated as text."
                ),
            },
            "title": {
                "type": "string",
                "description": "Optional short title for the podcast file.",
            },
            "language": {
                "type": "string",
                "description": "ISO code ('nl', 'en'). Defaults to config language.",
            },
            "style_hint": {
                "type": "string",
                "description": (
                    "Optional steering for tone, e.g. 'keep it light', 'focus on "
                    "technical details', 'max 3 minuten', 'for a marketer'."
                ),
            },
        },
        "required": ["source"],
    }

    _URL_RE = re.compile(r"^https?://", re.IGNORECASE)
    _MAX_SOURCE_CHARS = 40_000

    def __init__(
        self,
        workspace: Path,
        config: PodcastToolConfig,
        provider: LLMProvider,
        model: str,
        tts_provider: TTSProvider | None = None,
        web_proxy: str | None = None,
    ):
        self.workspace = workspace
        self.config = config
        self.provider = provider
        self.model = model
        self.tts = tts_provider
        self._web_fetcher = WebFetchTool(proxy=web_proxy)

    async def execute(
        self,
        source: str,
        title: str | None = None,
        language: str | None = None,
        style_hint: str | None = None,
        **_: Any,
    ) -> str:
        if not self.config.enabled:
            return (
                "Error: podcast tool is disabled. Enable it in config under "
                "tools.podcast.enabled = true."
            )
        if not self.tts:
            return (
                "Error: no TTS provider configured. Set providers.openai.apiKey "
                "and enable tools.podcast in config."
            )
        if not shutil.which("ffmpeg"):
            return (
                "Error: ffmpeg is required to assemble the podcast but was not found "
                "on PATH. Install ffmpeg (apt install -y ffmpeg) and try again."
            )
        if len(self.config.hosts) < 2:
            return "Error: podcast tool requires at least 2 hosts configured."

        source = (source or "").strip()
        if not source:
            return "Error: 'source' is empty."

        content, resolved_title = await self._resolve_source(source)
        if content is None:
            return "Error: could not extract any content from source."
        if not resolved_title and title:
            resolved_title = title

        lang = (language or self.config.language or "nl").lower()

        logger.info(
            "Podcast: generating script from {} chars of source (lang={})",
            len(content), lang,
        )
        script = await self._generate_script(
            content=content,
            language=lang,
            style_hint=style_hint,
            title=resolved_title,
        )
        if not script:
            return "Error: script generation returned no segments."

        media_dir = self.workspace / "media" / "podcasts"
        media_dir.mkdir(parents=True, exist_ok=True)
        slug = _slugify(title or resolved_title or "podcast")[:48] or "podcast"
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        workdir = media_dir / f"{timestamp}-{slug}"
        workdir.mkdir(parents=True, exist_ok=True)

        try:
            segment_paths = await self._synthesize_segments(script, workdir)
        except Exception as e:
            logger.exception("Podcast TTS failed")
            return f"Error: TTS synthesis failed: {e}"

        ext = self.config.audio_format if self.config.audio_format != "opus" else "ogg"
        final_path = media_dir / f"{timestamp}-{slug}.{ext}"

        try:
            await _ffmpeg_concat(segment_paths, final_path, self.config.audio_format)
        except Exception as e:
            logger.exception("Podcast concat failed")
            return f"Error: audio concat failed: {e}"

        # Clean up per-segment files; keep final output.
        for p in segment_paths:
            try:
                p.unlink()
            except OSError:
                pass
        try:
            workdir.rmdir()
        except OSError:
            pass

        duration_est = sum(len(line["text"]) for line in script) / 15.0  # ~15 chars/sec
        logger.info(
            "Podcast ready: {} ({} segments, ~{:.0f}s)",
            final_path, len(segment_paths), duration_est,
        )
        return (
            f"Podcast saved to {final_path}. "
            f"Call the `message` tool with media=[\"{final_path}\"] to deliver it. "
            f"Title: {resolved_title or slug}. ~{int(duration_est)} seconds, "
            f"{len(segment_paths)} dialogue lines."
        )

    async def _resolve_source(self, source: str) -> tuple[str | None, str]:
        """Return (content, title). content is None on failure."""
        if not self._URL_RE.match(source):
            return source[: self._MAX_SOURCE_CHARS], ""

        raw = await self._web_fetcher.execute(url=source, extractMode="markdown")
        if not isinstance(raw, str):
            logger.warning("web_fetch returned non-string for podcast source")
            return None, ""

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return raw[: self._MAX_SOURCE_CHARS], ""

        if isinstance(data, dict) and "error" in data:
            logger.warning("web_fetch error for podcast source: {}", data.get("error"))
            return None, ""

        text = (data.get("text") or "").strip() if isinstance(data, dict) else ""
        if not text:
            return None, ""

        title = ""
        first_line = text.splitlines()[0].strip()
        if first_line.startswith("# "):
            title = first_line[2:].strip()
        return text[: self._MAX_SOURCE_CHARS], title

    async def _generate_script(
        self,
        content: str,
        language: str,
        style_hint: str | None,
        title: str,
    ) -> list[dict[str, str]]:
        hosts = self.config.hosts
        host_block = "\n".join(
            f"- {h.name}: {h.style or 'Natural podcast host delivery.'}"
            for h in hosts
        )
        lang_label = {"nl": "Dutch (Nederlands)", "en": "English"}.get(language, language)
        style_line = f"\nAdditional direction: {style_hint}" if style_hint else ""
        title_line = f"\nWorking title: {title}" if title else ""

        system = (
            "You are a podcast scriptwriter. Produce a natural, engaging two-host "
            "dialogue that summarizes the provided source for a busy listener. "
            "Avoid filler, no introductions longer than one sentence, no outros "
            "longer than one sentence. Keep each spoken line short (max 2 sentences). "
            "Output valid JSON ONLY: an array of objects with keys 'host' (one of: "
            + ", ".join(h.name for h in hosts)
            + ") and 'text'. No markdown, no code fences, no commentary."
        )
        user = (
            f"Target language: {lang_label}.\n"
            f"Hosts and their voices:\n{host_block}"
            f"{title_line}{style_line}\n\n"
            "Length should fit the substance of the source — short sources get a "
            "short podcast (roughly 90-150 seconds), dense longreads may run up to "
            "5-6 minutes. Never pad.\n\n"
            "SOURCE:\n"
            + content
        )

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        response = await self.provider.chat(
            messages=messages,
            model=self.model,
            max_tokens=2048,
            temperature=0.7,
        )
        text = (response.content or "").strip()

        # Strip accidental code fences
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Podcast script not valid JSON; attempting salvage")
            match = re.search(r"\[[\s\S]*\]", text)
            if not match:
                return []
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                return []

        if not isinstance(parsed, list):
            return []

        host_names = {h.name for h in hosts}
        cleaned: list[dict[str, str]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            host = str(item.get("host", "")).strip()
            line_text = str(item.get("text", "")).strip()
            if not host or not line_text:
                continue
            if host not in host_names:
                host = hosts[len(cleaned) % len(hosts)].name
            cleaned.append({"host": host, "text": line_text})
        return cleaned

    async def _synthesize_segments(
        self,
        script: list[dict[str, str]],
        workdir: Path,
    ) -> list[Path]:
        host_by_name = {h.name: h for h in self.config.hosts}
        ext = "opus" if self.config.audio_format == "opus" else self.config.audio_format
        paths: list[Path] = []

        semaphore = asyncio.Semaphore(3)

        async def _one(idx: int, line: dict[str, str]) -> Path:
            host = host_by_name.get(line["host"]) or self.config.hosts[0]
            out = workdir / f"{idx:04d}-{_slugify(host.name)}.{ext}"
            async with semaphore:
                assert self.tts is not None  # guarded in execute()
                await self.tts.synthesize(
                    text=line["text"],
                    voice=host.voice,
                    output_path=out,
                    instructions=host.style or None,
                )
            return out

        results = await asyncio.gather(
            *[_one(i, line) for i, line in enumerate(script)]
        )
        paths.extend(results)
        return paths


async def _ffmpeg_concat(
    inputs: list[Path],
    output: Path,
    audio_format: str,
) -> None:
    """Concatenate audio files via ffmpeg's concat demuxer."""
    if not inputs:
        raise ValueError("No input segments to concat.")

    list_file = output.with_suffix(output.suffix + ".list")
    list_file.write_text(
        "\n".join(f"file '{p.resolve().as_posix()}'" for p in inputs) + "\n",
        encoding="utf-8",
    )

    codec_args: list[str]
    if audio_format == "opus":
        codec_args = ["-c:a", "libopus", "-b:a", "24k"]
    elif audio_format == "mp3":
        codec_args = ["-c:a", "libmp3lame", "-b:a", "64k"]
    else:
        codec_args = ["-c:a", "copy"]

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        *codec_args,
        str(output),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    try:
        list_file.unlink()
    except OSError:
        pass
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg concat failed (code {proc.returncode}): "
            f"{stderr.decode('utf-8', errors='replace')[:400]}"
        )


def _slugify(text: str) -> str:
    """Simple slugifier for filenames."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")
