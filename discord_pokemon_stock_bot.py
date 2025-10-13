"""
Discord Stock Tracker Bot for bol.com & MediaMarkt (Pokémon pages)
-----------------------------------------------------------------
Features
- Track any product URL from bol.com or mediamarkt.nl
- Periodically checks stock and posts alerts on status changes (e.g., OutOfStock -> InStock)
- Commands to add/remove/list URLs, set check interval, and choose the alert channel
- Gentle scraping with realistic headers, random jitter, and JSON-LD parsing when possible

Quick Start
1) Python 3.10+ recommended
2) Install deps:  
   pip install -U discord.py aiohttp beautifulsoup4 python-dotenv
3) Create a .env file next to this script with:
   DISCORD_TOKEN=YOUR_BOT_TOKEN
   # Optional: set a default channel id (numeric) for alerts
   # ALERT_CHANNEL_ID=123456789012345678
4) Run the bot:
   python discord_pokemon_stock_bot.py

In Discord (bot must be invited to your server with message & channel permissions):
- !channel #alerts                 -> set the alert channel
- !interval 5                      -> set check interval to 5 minutes
- !track add <url> [nickname]      -> start tracking a product URL
- !track remove <url-or-nickname>  -> stop tracking
- !track list                      -> list tracked URLs
- !ping                            -> health check

Notes
- The bot parses structured data (JSON-LD) for offer availability when possible.
- Fallback to keyword scanning for Dutch phrases like "Op voorraad" / "Niet op voorraad".
- Be respectful: default interval is 5 minutes, with random jitter to avoid hammering.
- This script stores state in stock_state.json in the same folder.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup
import discord
from discord.ext import commands
from dotenv import load_dotenv

STATE_FILE = "stock_state.json"
DEFAULT_INTERVAL_SEC = 5 * 60  # 5 minutes
MIN_INTERVAL_SEC = 60  # safety floor: 1 minute
REQUEST_JITTER_BETWEEN_ITEMS = (1.5, 4.0)  # seconds between requests per item
ROUND_INTERVAL_JITTER = (0, 10)  # extra seconds added after a full sweep

# Dutch stock keywords (fallback if JSON-LD isn't present)
IN_STOCK_KEYWORDS = [
    "op voorraad", "op=voorraad", "online op voorraad", "direct leverbaar", "morgen in huis",
    "in stock", "available"
]
OUT_STOCK_KEYWORDS = [
    "niet op voorraad", "uitverkocht", "tijdelijk uitverkocht", "niet beschikbaar","niet leverbaar",
    "currently unavailable", "out of stock"
]
PREORDER_KEYWORDS = ["pre-order", "preorder", "pre-orderen", "verwacht"]

# Regex for JSON-LD availability
AVAIL_PATTERN = re.compile(r"\bavailability\b\"?\s*:\s*\"(.*?)\"", re.IGNORECASE)
NAME_PATTERN = re.compile(r"\b\"name\"\s*:\s*\"(.*?)\"")

@dataclass
class TrackedItem:
    url: str
    nickname: Optional[str] = None
    last_status: Optional[str] = None  # e.g., InStock / OutOfStock / PreOrder / Unknown
    last_title: Optional[str] = None
    last_checked: Optional[float] = None

@dataclass
class BotConfig:
    interval_sec: int = DEFAULT_INTERVAL_SEC
    alert_channel_id: Optional[int] = None
    items: Dict[str, TrackedItem] = None  # keyed by URL

    def to_dict(self):
        return {
            "interval_sec": self.interval_sec,
            "alert_channel_id": self.alert_channel_id,
            "items": {url: asdict(item) for url, item in (self.items or {}).items()},
        }

    @staticmethod
    def from_dict(d: dict) -> "BotConfig":
        items = {url: TrackedItem(**it) for url, it in (d.get("items") or {}).items()}
        return BotConfig(
            interval_sec=int(d.get("interval_sec", DEFAULT_INTERVAL_SEC)),
            alert_channel_id=d.get("alert_channel_id"),
            items=items,
        )

# ------------------------- Persistence -------------------------

def load_state() -> BotConfig:
    if not os.path.exists(STATE_FILE):
        return BotConfig(items={})
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return BotConfig.from_dict(data)
    except Exception:
        return BotConfig(items={})


def save_state(cfg: BotConfig):
    tmp = cfg.to_dict()
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(tmp, f, ensure_ascii=False, indent=2)

# ------------------------- Stock Checking -------------------------

async def fetch_html(session: aiohttp.ClientSession, url: str) -> Optional[str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            if resp.status != 200:
                return None
            return await resp.text()
    except Exception:
        return None


def parse_jsonld_availability(html: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (availability, name) when found via JSON-LD, else (None, None).
    Availability normalized to InStock / OutOfStock / PreOrder when possible.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        scripts = soup.find_all("script", {"type": "application/ld+json"})
        for sc in scripts:
            content = sc.string
            if not content:
                content = sc.text
            if not content:
                continue
            # Try simple regex first (fast path)
            m = AVAIL_PATTERN.search(content)
            name_m = NAME_PATTERN.search(content)
            name = name_m.group(1) if name_m else None
            if m:
                raw = m.group(1).lower()
                if "instock" in raw:
                    return "InStock", name
                if "outofstock" in raw:
                    return "OutOfStock", name
                if "preorder" in raw or "pre-order" in raw:
                    return "PreOrder", name
            # Fallback to JSON parsing (handles objects/arrays)
            try:
                data = json.loads(content)
            except Exception:
                continue
            def extract(d):
                if isinstance(d, dict):
                    nm = d.get("name") if isinstance(d.get("name"), str) else None
                    offers = d.get("offers")
                    if isinstance(offers, dict):
                        avail = offers.get("availability")
                        if isinstance(avail, str):
                            return avail, nm
                    if isinstance(offers, list):
                        for o in offers:
                            if isinstance(o, dict) and isinstance(o.get("availability"), str):
                                return o.get("availability"), nm
                if isinstance(d, list):
                    for el in d:
                        res = extract(el)
                        if res:
                            return res
                return None
            found = extract(data)
            if found:
                avail_raw, nm = found
                avail_raw_l = str(avail_raw).lower()
                if "instock" in avail_raw_l:
                    return "InStock", nm
                if "outofstock" in avail_raw_l:
                    return "OutOfStock", nm
                if "preorder" in avail_raw_l or "pre-order" in avail_raw_l:
                    return "PreOrder", nm
                return "Unknown", nm
    except Exception:
        pass
    return None, None


def keyword_status_fallback(html: str) -> str:
    body = BeautifulSoup(html, "html.parser").get_text(" ").lower()
    def any_kw(words):
        return any(w in body for w in words)
    if any_kw(IN_STOCK_KEYWORDS):
        return "InStock"
    if any_kw(OUT_STOCK_KEYWORDS):
        return "OutOfStock"
    if any_kw(PREORDER_KEYWORDS):
        return "PreOrder"
    return "Unknown"


def extract_title(html: str) -> Optional[str]:
    try:
        soup = BeautifulSoup(html, "html.parser")
        if soup.title and soup.title.string:
            return soup.title.string.strip()
    except Exception:
        pass
    return None


async def check_url(session: aiohttp.ClientSession, url: str) -> Tuple[str, Optional[str]]:
    """Return (status, title). status in {InStock, OutOfStock, PreOrder, Unknown}.
    Gentle parsing via JSON-LD with keyword fallback.
    """
    html = await fetch_html(session, url)
    if not html:
        return "Unknown", None

    status, name = parse_jsonld_availability(html)
    if not status or status == "Unknown":
        status = keyword_status_fallback(html)
    title = name or extract_title(html)
    return status, title


# ------------------------- Discord Bot -------------------------

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

cfg = load_state()
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DEFAULT_ALERT_CHANNEL_ID = os.getenv("ALERT_CHANNEL_ID")
if DEFAULT_ALERT_CHANNEL_ID and not cfg.alert_channel_id:
    try:
        cfg.alert_channel_id = int(DEFAULT_ALERT_CHANNEL_ID)
    except Exception:
        pass

_monitor_task: Optional[asyncio.Task] = None


def human_domain(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ""


def pretty_item_line(item: TrackedItem) -> str:
    nick = f" ({item.nickname})" if item.nickname else ""
    status = item.last_status or "?"
    dom = human_domain(item.url)
    return f"- {item.url}{nick} — **{status}** [{dom}]"


async def send_alert(url: str, title: Optional[str], old: Optional[str], new: str):
    if not cfg.alert_channel_id:
        return
    channel = bot.get_channel(cfg.alert_channel_id)
    if not channel:
        return
    dom = human_domain(url)
    name = title or url
    old_s = old or "Unknown"
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    await channel.send(
        f"**Stock change detected**\n"
        f"**{name}**\n"
        f"Site: `{dom}`\n"
        f"Status: `{old_s}` → **`{new}`**\n"
        f"Link: {url}\n"
        f"At: {ts}"
    )


async def monitor_loop():
    await bot.wait_until_ready()
    await asyncio.sleep(3)
    session_timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=session_timeout) as session:
        while not bot.is_closed():
            # sweep all items
            for i, (url, item) in enumerate(list(cfg.items.items())):
                status, title = await check_url(session, url)
                changed = (status != item.last_status) and (item.last_status is not None)
                # First observation: don't spam unless it's InStock
                first_time = item.last_status is None
                if first_time and status == "InStock":
                    changed = True
                # save
                item.last_status = status
                item.last_title = title or item.last_title
                item.last_checked = time.time()
                cfg.items[url] = item
                save_state(cfg)
                if changed:
                    await send_alert(url, item.last_title, old=item.last_status if not first_time else None, new=status)
                # polite delay between items
                await asyncio.sleep(random.uniform(*REQUEST_JITTER_BETWEEN_ITEMS))
            # wait for next round
            base = max(cfg.interval_sec, MIN_INTERVAL_SEC)
            await asyncio.sleep(base + random.uniform(*ROUND_INTERVAL_JITTER))


@bot.event
async def on_ready():
    global _monitor_task
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    if _monitor_task is None or _monitor_task.done():
        _monitor_task = asyncio.create_task(monitor_loop())


# ------------------------- Commands -------------------------

@bot.command(name="help")
async def _help(ctx: commands.Context):
    msg = (
        "**Commands**\n"
        "!ping — bot health\n"
        "!channel #channel — set the alert channel\n"
        "!interval <minutes> — set check interval (min 1)\n"
        "!track add <url> [nickname] — start tracking a URL\n"
        "!track remove <url-or-nickname> — stop tracking\n"
        "!track list — show tracked URLs\n"
    )
    await ctx.send(msg)


@bot.command(name="ping")
async def ping(ctx: commands.Context):
    await ctx.send("Pong! ✅")


@bot.command(name="channel")
async def set_channel(ctx: commands.Context, channel: discord.TextChannel):
    cfg.alert_channel_id = channel.id
    save_state(cfg)
    await ctx.send(f"Alerts will be sent to {channel.mention}.")


@bot.command(name="interval")
async def set_interval(ctx: commands.Context, minutes: int):
    minutes = max(1, minutes)
    cfg.interval_sec = minutes * 60
    save_state(cfg)
    await ctx.send(f"Check interval set to {minutes} minute(s).")


@bot.group(name="track", invoke_without_command=True)
async def track_root(ctx: commands.Context):
    await _help(ctx)


@track_root.command(name="add")
async def track_add(ctx: commands.Context, url: str, nickname: Optional[str] = None):
    if not ("bol.com" in url or "mediamarkt" in url):
        await ctx.send("Please provide a bol.com or mediamarkt.nl product URL.")
        return
    if cfg.items is None:
        cfg.items = {}
    if url in cfg.items:
        await ctx.send("This URL is already being tracked.")
        return
    cfg.items[url] = TrackedItem(url=url, nickname=nickname)
    save_state(cfg)
    await ctx.send(f"Added tracking for: {url} {'('+nickname+')' if nickname else ''}")


@track_root.command(name="remove")
async def track_remove(ctx: commands.Context, identifier: str):
    # identifier may be URL or nickname
    if identifier in cfg.items:
        cfg.items.pop(identifier, None)
        save_state(cfg)
        await ctx.send(f"Removed: {identifier}")
        return
    # find by nickname
    to_del = None
    for url, item in cfg.items.items():
        if item.nickname and item.nickname.lower() == identifier.lower():
            to_del = url
            break
    if to_del:
        cfg.items.pop(to_del, None)
        save_state(cfg)
        await ctx.send(f"Removed: {identifier}")
    else:
        await ctx.send("No matching URL or nickname found.")


@track_root.command(name="list")
async def track_list(ctx: commands.Context):
    if not cfg.items:
        await ctx.send("No URLs are being tracked yet. Use `!track add <url>`.")
        return
    lines = [pretty_item_line(it) for it in cfg.items.values()]
    await ctx.send("Tracked items:\n" + "\n".join(lines))


# ------------------------- Run -------------------------
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN not set in environment or .env")
    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        pass
