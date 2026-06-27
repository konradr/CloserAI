"""Recall.ai — create a Google Meet bot, receive transcript, speak audio in-call."""

from __future__ import annotations

import base64
import os

import httpx

from crm.config import load_env

from . import slng

load_env()

REGION = os.getenv("RECALL_REGION", "eu-central-1")
BASE = f"https://{REGION}.recall.ai/api/v1"
KEY = os.getenv("RECALL_API_KEY", "")
HEADERS = {"Authorization": f"Token {KEY}", "Content-Type": "application/json"}


async def create_bot(meeting_url: str, webhook_url: str | None = None, bot_name: str = "CloserAI") -> str:
    """Create a bot that joins the Meet with audio output (+ optional live transcript).

    webhook_url is only needed for real-time transcript (requires a public URL).
    On localhost, omit it — the bot still joins and can speak on demand.
    """
    silent_b64 = base64.b64encode(await slng.silent_mp3()).decode()
    recording_config: dict = {
        "transcript": {
            "provider": {
                "recallai_streaming": {
                    "mode": "prioritize_low_latency",
                    "language_code": "en",
                }
            }
        }
    }
    if webhook_url:
        recording_config["realtime_endpoints"] = [
            {"type": "webhook", "url": webhook_url, "events": ["transcript.data"]}
        ]
    payload = {
        "meeting_url": meeting_url,
        "bot_name": bot_name,
        "recording_config": recording_config,
        # Required to enable the on-demand Output Audio endpoint (uses a silent clip).
        "automatic_audio_output": {
            "in_call_recording": {"data": {"kind": "mp3", "b64_data": silent_b64}}
        },
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(f"{BASE}/bot/", headers=HEADERS, json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Recall create_bot {resp.status_code}: {resp.text[:300]} "
                f"(check the meeting_url is a real Google Meet link like "
                f"https://meet.google.com/abc-defg-hij)"
            )
        return resp.json()["id"]


async def output_audio(bot_id: str, mp3_bytes: bytes) -> bool:
    """Inject MP3 audio into the meeting (the bot 'speaks' it)."""
    b64 = base64.b64encode(mp3_bytes).decode()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{BASE}/bot/{bot_id}/output_audio/",
            headers=HEADERS,
            json={"kind": "mp3", "b64_data": b64},
        )
        if resp.status_code >= 400:
            print(f"[recall] output_audio {resp.status_code}: {resp.text[:200]}")
            return False
        return True


async def leave_call(bot_id: str) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        await client.post(f"{BASE}/bot/{bot_id}/leave_call/", headers=HEADERS)
