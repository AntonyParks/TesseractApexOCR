"""Twitch Helix API helpers — stream discovery for Apex Legends."""

import os

import requests

from config import STREAM_BLOCKLIST

_TOKEN_URL   = "https://id.twitch.tv/oauth2/token"
_STREAMS_URL = "https://api.twitch.tv/helix/streams"
_GAMES_URL   = "https://api.twitch.tv/helix/games"

_APEX_GAME_NAME = "Apex Legends"

# Keywords in stream titles that suggest ranked play
_RANKED_KEYWORDS = {"ranked", "rank", " rp", "pred", "master", "diamond", "platinum"}


def _title_suggests_ranked(title: str) -> bool:
    low = title.lower()
    return any(kw in low for kw in _RANKED_KEYWORDS)


def _get_app_token(client_id: str, client_secret: str) -> str:
    resp = requests.post(_TOKEN_URL, params={
        "client_id":     client_id,
        "client_secret": client_secret,
        "grant_type":    "client_credentials",
    }, timeout=10)
    resp.raise_for_status()
    return resp.json()["access_token"]


def _get_game_id(headers: dict, game_name: str) -> str:
    """Look up the Twitch game ID by exact name."""
    resp = requests.get(_GAMES_URL, headers=headers,
                        params={"name": game_name}, timeout=10)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    if not data:
        raise ValueError(f"Game '{game_name}' not found on Twitch")
    return data[0]["id"]


def get_top_apex_streams(n: int = 20, ranked_only: bool = False) -> list[str]:
    """Return up to *n* Twitch usernames currently streaming Apex, sorted by viewers.

    Args:
        n: Maximum number of usernames to return.
        ranked_only: If True, only include streams whose title suggests ranked play.

    Requires env vars TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET.
    """
    client_id     = os.environ.get("TWITCH_CLIENT_ID", "")
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise ValueError(
            "Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET environment variables. "
            "Get credentials at https://dev.twitch.tv/console"
        )

    token = _get_app_token(client_id, client_secret)
    headers = {
        "Client-Id":     client_id,
        "Authorization": f"Bearer {token}",
    }

    game_id = _get_game_id(headers, _APEX_GAME_NAME)

    usernames = []
    cursor    = None
    # When ranked_only, over-fetch to compensate for filtered-out streams
    fetch_size = n * 4 if ranked_only else n

    while len(usernames) < n:
        params: dict = {
            "game_id": game_id,
            "first":   min(fetch_size - len(usernames), 100),
        }
        if cursor:
            params["after"] = cursor

        resp = requests.get(_STREAMS_URL, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        body = resp.json()

        for stream in body.get("data", []):
            login = stream["user_login"].lower()
            if login in STREAM_BLOCKLIST:
                continue
            if ranked_only and not _title_suggests_ranked(stream.get("title", "")):
                continue
            usernames.append(login)
            if len(usernames) >= n:
                break

        cursor = body.get("pagination", {}).get("cursor")
        if not cursor or not body.get("data"):
            break

    return usernames[:n]
