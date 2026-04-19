from __future__ import annotations

import json
import os
import time
import base64
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)
_spotify_token_cache: dict[str, object] = {"access_token": "", "expires_at": 0}

def _safe_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return default

def get_spotify_access_token() -> tuple[str | None, str | None]:
    """Return a valid Spotify access token."""
    refresh_token = os.getenv("SPOTIFY_REFRESH_TOKEN", "").strip()
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
    now = int(time.time())

    if refresh_token and client_id and client_secret:
        try:
            cached_token = str(_spotify_token_cache.get("access_token", "") or "")
            expires_at = int(_spotify_token_cache.get("expires_at", 0) or 0)
        except Exception:
            cached_token = ""
            expires_at = 0

        if cached_token and expires_at and now < (expires_at - 15):
            return cached_token, None

        token_url = "https://accounts.spotify.com/api/token"
        body = f"grant_type=refresh_token&refresh_token={quote(refresh_token)}".encode("utf-8")
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
        req = Request(
            token_url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(req, timeout=15) as res:
                raw = res.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw else {}
            token = str(payload.get("access_token", "") or "").strip()
            if not token:
                detail = str(payload.get("error_description") or payload.get("error") or "refresh_failed")
                return None, f"spotify_refresh_failed:{detail[:200]}"
            expires_in = int(payload.get("expires_in", 3600) or 3600)
            _spotify_token_cache["access_token"] = token
            _spotify_token_cache["expires_at"] = int(time.time()) + expires_in
            return token, None
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")
            except Exception:
                detail = str(exc)
            return None, f"spotify_token_http_error:{int(getattr(exc, 'code', 500))}:{detail[:200]}"
        except URLError as exc:
            return None, f"spotify_token_url_error:{exc}"
        except Exception as exc:
            return None, f"spotify_token_error:{exc}"

    token = os.getenv("SPOTIFY_ACCESS_TOKEN", "").strip()
    if token:
        return token, None
    return None, "spotify_access_token_missing"


def spotify_search_track_uri(query: str) -> tuple[str | None, str | None]:
    token, tok_err = get_spotify_access_token()
    if tok_err is not None:
        return None, tok_err
    token = str(token or "").strip()
    if not token:
        return None, "spotify_access_token_missing"

    timeout_sec = max(5, _safe_int("MUSIC_INTENT_SPOTIFY_TIMEOUT_SEC", 15))
    # Pre-process query to remove noise punctuation that might hinder Spotify search
    search_q = query
    for char in ("!", "！", ",", "，", "、", "?", "？"):
        search_q = search_q.replace(char, " ")
    
    url = f"https://api.spotify.com/v1/search?q={quote(search_q.strip())}&type=track&limit=1"
    req = Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=timeout_sec) as res:
            raw = res.read().decode("utf-8", errors="replace")
        payload = json.loads(raw) if raw else {}
        items = (((payload.get("tracks") or {}).get("items")) or [])
        if not items:
            return None, "track_not_found"
        return str(items[0].get("uri", "")).strip() or None, None
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        return None, f"spotify_search_http_error:{int(getattr(exc, 'code', 500))}:{detail[:200]}"
    except URLError as exc:
        return None, f"spotify_search_url_error:{exc}"
    except Exception as exc:
        return None, f"spotify_search_error:{exc}"


def spotify_add_to_queue(track_uri: str) -> str | None:
    token, tok_err = get_spotify_access_token()
    if tok_err is not None:
        return tok_err
    token = str(token or "").strip()
    if not token:
        return "spotify_access_token_missing"

    timeout_sec = max(5, _safe_int("MUSIC_INTENT_SPOTIFY_TIMEOUT_SEC", 15))
    device_id = os.getenv("SPOTIFY_DEVICE_ID", "").strip()
    url = f"https://api.spotify.com/v1/me/player/queue?uri={quote(track_uri)}"
    if device_id:
        url += f"&device_id={quote(device_id)}"
    req = Request(
        url,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=timeout_sec):
            pass
        return None
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        return f"spotify_queue_http_error:{int(getattr(exc, 'code', 500))}:{detail[:200]}"
    except URLError as exc:
        return f"spotify_queue_url_error:{exc}"
    except Exception as exc:
        return f"spotify_queue_error:{exc}"


def add_to_jam(query: str) -> str | None:
    """Search for a track and add it to the active Spotify queue."""
    if not query.strip():
        return "query_empty"
    track_uri, err = spotify_search_track_uri(query)
    if err:
        return err
    if not track_uri:
        return "track_not_found"
    return spotify_add_to_queue(track_uri)


def weather_recommend() -> str | None:
    """Fetch weather and add a recommended track to queue."""
    api_key = os.getenv("OPENWEATHERMAP_API_KEY", "").strip()
    if not api_key:
        return "openweathermap_api_key_missing"
    city = os.getenv("WEATHER_RECOMMEND_CITY", "Tokyo").strip()

    url = f"https://api.openweathermap.org/data/2.5/weather?q={quote(city)}&appid={api_key}&units=metric"
    req = Request(url, method="GET")
    try:
        with urlopen(req, timeout=10) as res:
            data = json.loads(res.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        return f"weather_api_http_error:{int(getattr(exc, 'code', 500))}:{detail[:200]}"
    except URLError as exc:
        return f"weather_api_url_error:{exc}"
    except Exception as exc:
        return f"weather_api_error:{exc}"

    weather_desc = data.get("weather", [{}])[0].get("main", "Clear").lower()

    # Determine music context based on weather
    if "rain" in weather_desc or "drizzle" in weather_desc or "thunderstorm" in weather_desc:
        query = "rain jazz lofi"
    elif "cloud" in weather_desc:
        query = "chill relax acoustic"
    elif "snow" in weather_desc:
        query = "snow winter jazz"
    elif "clear" in weather_desc:
        query = "sunny upbeat pop"
    else:
        query = "chill workspace"

    logger.info("Weather is %s, recommending music query: %s", weather_desc, query)
    return add_to_jam(query)
