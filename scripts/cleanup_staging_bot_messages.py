"""
Staging Discord channel cleanup — periodically recreates the channel
to clear all messages. Uses delete+recreate instead of bulk message
deletion to avoid Discord 429 rate limits.

The staging bot needs a restart after cleanup to pick up the new channel_id.
"""
import asyncio
import json
import os
import sys
import traceback
from datetime import datetime, timezone

import discord


CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config.dev.json")
DEFAULT_INTERVAL_HOURS = 24.0
MIN_INTERVAL_HOURS = 6.0


def cleanup_interval_hours() -> float:
    raw = os.environ.get("CLEANUP_INTERVAL_HOURS", str(DEFAULT_INTERVAL_HOURS))
    try:
        interval = float(raw)
    except ValueError:
        raise SystemExit(f"[FATAL] CLEANUP_INTERVAL_HOURS must be numeric, got {raw!r}")
    if interval < MIN_INTERVAL_HOURS:
        raise SystemExit(
            f"[FATAL] CLEANUP_INTERVAL_HOURS={interval} is too low; "
            f"minimum is {MIN_INTERVAL_HOURS}h"
        )
    return interval


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


async def do_cleanup(client: discord.Client) -> None:
    cfg = load_config()
    channel_id = int(cfg["discord"]["channel_id"])

    channel = await client.fetch_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        print(f"[ERROR] Channel {channel_id} is not a text channel")
        return

    ts = datetime.now(timezone.utc).isoformat()
    print(f"[{ts}] Recreating channel: {channel.name} ({channel.id})")

    # Snapshot settings from old channel
    name = channel.name
    topic = channel.topic
    nsfw = channel.nsfw
    slowmode_delay = channel.slowmode_delay
    position = channel.position
    category = channel.category
    overwrites = dict(channel.overwrites)

    guild = channel.guild

    # Create new channel first — if this fails, old channel is preserved
    print("  Creating new channel ...")
    new_channel = await guild.create_text_channel(
        name=name,
        topic=topic,
        nsfw=nsfw,
        slowmode_delay=slowmode_delay,
        position=position,
        category=category,
        overwrites=overwrites,
        reason="Staging cleanup: periodic channel refresh",
    )
    print(f"  New channel created: {new_channel.name} ({new_channel.id})")

    # Delete old channel
    print("  Deleting old channel ...")
    await channel.delete(reason="Staging cleanup: periodic channel refresh")
    print(f"  Old channel deleted: {channel.name} ({channel.id})")

    # Update config
    cfg["discord"]["channel_id"] = str(new_channel.id)
    save_config(cfg)
    print(f"  Config updated: channel_id = {new_channel.id}")

    print("  Done — restart staging bot to pick up new channel_id.")


async def main_loop() -> None:
    interval_hours = cleanup_interval_hours()
    cfg = load_config()
    token = cfg["discord"]["token"]

    intents = discord.Intents.default()
    intents.guilds = True

    while True:
        client = discord.Client(intents=intents)
        try:
            @client.event
            async def on_ready():
                try:
                    await do_cleanup(client)
                except Exception:
                    traceback.print_exc()
                finally:
                    await client.close()

            await client.start(token)
        except discord.LoginFailure:
            print("[FATAL] Discord token is invalid; exiting instead of retrying")
            await client.close()
            raise
        except Exception:
            traceback.print_exc()
            await client.close()

        next_ts = datetime.now(timezone.utc).isoformat()
        print(f"[{next_ts}] Next cleanup in {interval_hours}h")
        await asyncio.sleep(interval_hours * 3600)


if __name__ == "__main__":
    asyncio.run(main_loop())
