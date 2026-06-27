"""SLNG voice — text to speech, returned as MP3 (for Recall.ai output)."""

from __future__ import annotations

import asyncio
import os

import httpx

from crm.config import load_env

load_env()

SLNG_API_KEY = os.getenv("SLNG_API_KEY", "")
SLNG_VOICE = os.getenv("SLNG_VOICE", "aura-2-thalia-en")
TTS_URL = "https://api.slng.ai/v1/tts/slng/deepgram/aura:2-en"


async def _wav_bytes(text: str) -> bytes:
    """Synthesize speech as WAV via SLNG (the format this model supports)."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            TTS_URL,
            headers={
                "Authorization": f"Bearer {SLNG_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": SLNG_VOICE,
                "text": text,
                "encoding": "linear16",
                "container": "wav",
            },
        )
        resp.raise_for_status()
        return resp.content


async def _wav_to_mp3(wav: bytes) -> bytes:
    """Convert WAV -> MP3 with ffmpeg (Recall.ai output audio requires MP3)."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0", "-f", "mp3", "-ar", "44100", "-ac", "1", "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(input=wav)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {err.decode()[:200]}")
    return out


async def synthesize_mp3(text: str) -> bytes:
    """Text -> MP3 bytes, ready for Recall.ai output_audio."""
    wav = await _wav_bytes(text)
    return await _wav_to_mp3(wav)


_silent_mp3: bytes | None = None


async def silent_mp3() -> bytes:
    """A tiny silent MP3 clip — needed to enable Recall's output_audio endpoint."""
    global _silent_mp3
    if _silent_mp3 is None:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "0.3",
            "-f", "mp3", "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        _silent_mp3 = out
    return _silent_mp3
