from modules import get, helpers
from flask import Blueprint, Flask, request, redirect, render_template, Response
import config
from modules.logs import print_with_seperator
from modules import yt
from api.login import (
    extract_device_id,
    is_device_linked,
    any_session_linked,
    get_valid_access_token,
    get_logged_in_channel_id,
    fetch_personal_feed,
)
import os
import re
import time
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

video = Blueprint("video", __name__)

# prevents multiple simultaneous requests for the same video from spawning
# duplicate yt-dlp/ffmpeg pipelines in parallel
_download_locks = {}
_download_locks_guard = threading.Lock()
_download_semaphore = threading.Semaphore(2)  # max 2 simultaneous yt-dlp/ffmpeg jobs

def _get_download_lock(video_id):
    with _download_locks_guard:
        if video_id not in _download_locks:
            _download_locks[video_id] = threading.Lock()
        return _download_locks[video_id]

import subprocess
import json
import random
import html

_best_video_encoder = None
_encoder_detection_lock = threading.Lock()

def _detect_best_video_encoder():
    """Runs once, caches the result. Tests hardware encoders against a
    trivial 1-second dummy source instead of re-testing per real request,
    and instead of restarting a fresh yt-dlp download for every fallback
    attempt (which piping would otherwise require, since a failed
    encoder partway through consumes/breaks the piped source data)."""
    global _best_video_encoder

    with _encoder_detection_lock:
        if _best_video_encoder is not None:
            return _best_video_encoder

        candidates = [
            ["-c:v", "h264_nvenc", "-preset", "p1", "-profile:v", "baseline"],
            ["-c:v", "h264_qsv", "-preset", "veryfast", "-profile:v", "baseline"],
        ]

        for encoder_args in candidates:
            test_output = f"_encoder_test_{encoder_args[1]}.mp4"
            try:
                subprocess.run([
                    "ffmpeg", "-y",
                    "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=5",
                    *encoder_args,
                    "-t", "1",
                    test_output
                ], check=True, capture_output=True, timeout=15)
                print("ENCODER DETECTION: found working hardware encoder:", encoder_args[1], flush=True)
                _best_video_encoder = encoder_args
                return _best_video_encoder
            except Exception as e:
                print("ENCODER DETECTION: not available:", encoder_args[1], flush=True)
            finally:
                if os.path.exists(test_output):
                    os.remove(test_output)

        print("ENCODER DETECTION: no hardware encoder available, using software libx264", flush=True)
        _best_video_encoder = ["-c:v", "libx264", "-preset", "ultrafast", "-profile:v", "baseline"]
        return _best_video_encoder
import requests

from datetime import datetime
def safe_published(value):
    if not value:
        return 0

    if isinstance(value, int):
        return value

    if isinstance(value, str):
        try:
            return int(value)
        except:
            pass

        try:
            return int(datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").timestamp())
        except:
            return 0

    return 0

def safe_int(value):
    if not value:
        return 0

    if isinstance(value, int):
        return value

    if isinstance(value, str):
        value = value.replace(",", "").replace("views", "").strip()

        try:
            return int(value)
        except:
            return 0

    return 0

def enrich_view_counts(items, max_workers=10, force_refresh=False, max_enrich=30):
    """Fetch real stats (views, duration, published date, author/channel)
    for items missing them, concurrently instead of one-at-a-time —
    sequential per-item calls on a 20-30 item feed is what caused Top
    Rated to time out. Also used to backfill author/authorId when a
    fragile client-side parse (e.g. InnerTube's tileRenderer for
    Subscriptions) couldn't resolve the channel — Invidious's per-video
    metadata reliably has both, so this doubles as channel ID -> name
    translation wherever it's missing.

    force_refresh=True skips the disk cache entirely and always fetches
    live — used by Featured/Most Viewed/Top Rated so nothing in that
    pipeline is ever served stale, matching the original tuberepair
    tweak's always-fresh-reload behavior.

    max_enrich raises/lowers the hard cap below for callers that are
    scoped narrowly enough (e.g. one channel's Shorts) that a higher
    cap won't reintroduce the app-wide slowdown a low cap protects
    against elsewhere."""
    def _looks_like_raw_id(item):
        author = item.get("author") or ""
        return author.startswith("UC") and len(author) > 15

    to_fetch = [
        item for item in items
        if item and (
            not item.get("viewCount")
            or not item.get("lengthSeconds")
            or not item.get("author") or item.get("author") in ("Unknown", "unknown")
            or not item.get("authorId") or item.get("authorId") == "unknown"
            or _looks_like_raw_id(item)
            or force_refresh
        )
    ]
    print(f"ENRICH_VIEW_COUNTS: {len(items)} items total, {len(to_fetch)} need enrichment (force_refresh={force_refresh})", flush=True)

    # hard safety cap — 200 items needing enrichment (as seen on Most
    # Viewed) is what caused the timeout even with parallelization.
    # Anything beyond this just doesn't get enriched rather than
    # blocking the whole request.
    MAX_ENRICH = max_enrich
    if len(to_fetch) > MAX_ENRICH:
        print(f"ENRICH_VIEW_COUNTS: capping {len(to_fetch)} down to {MAX_ENRICH}", flush=True)
        to_fetch = to_fetch[:MAX_ENRICH]

    if not to_fetch:
        return items

    def _fetch(item):
        video_id = item.get("videoId")

        # check the EXISTING persistent disk-backed cache first — this
        # was already built for a different mechanism (fetch_ytdlp_metadata)
        # but enrich_view_counts() never used it, meaning every single
        # request re-fetched stats over the network even for videos seen
        # minutes ago in a different feed. This is almost certainly the
        # real cause of enrichment noticeably slowing things down.
        cached = metadata_cache.get(video_id) if video_id else None
        cache_is_fresh = (not force_refresh) and cached and (time.time() - cached.get("cachedAt", 0)) < 21600  # 6 hours

        if cache_is_fresh:
            if not item.get("viewCount"):
                item["viewCount"] = cached.get("viewCount", 0)
            if not item.get("lengthSeconds"):
                item["lengthSeconds"] = cached.get("lengthSeconds", 0)
            if not item.get("published"):
                item["published"] = cached.get("published", 0)
            if cached.get("author") and (not item.get("author") or item.get("author") in ("Unknown", "unknown") or _looks_like_raw_id(item)):
                item["author"] = cached["author"]
            if cached.get("authorId") and (not item.get("authorId") or item.get("authorId") == "unknown"):
                item["authorId"] = cached["authorId"]
            if cached.get("isLive"):
                item["_is_live"] = True
        else:
            try:
                # NOTE: this Invidious instance silently returns nothing when
                # given a multi-field comma-separated `fields=` parameter —
                # confirmed via a raw log capture. That's been the real cause
                # of enrichment quietly failing with zero errors this whole
                # time (no exception was ever thrown, `stats` was just empty).
                # Fetching the full object instead of a partial projection
                # actually works.
                stats = get.fetch(f"{config.URL}/api/v1/videos/{video_id}")
                if not stats:
                    print("STATS ENRICH: empty response for", video_id, flush=True)
                if stats:
                    is_live = stats.get("type") == "livestream"
                    if is_live:
                        item["_is_live"] = True

                    view_count = int(stats.get("viewCount") or 0)
                    length_seconds = int(stats.get("lengthSeconds") or 0)
                    published = int(stats.get("published") or 0)
                    real_author = stats.get("author") or ""
                    real_author_id = stats.get("authorId") or ""

                    if force_refresh or not item.get("viewCount"):
                        item["viewCount"] = view_count
                    if force_refresh or not item.get("lengthSeconds"):
                        item["lengthSeconds"] = length_seconds
                    if force_refresh or not item.get("published"):
                        item["published"] = published
                    if real_author and (not item.get("author") or item.get("author") in ("Unknown", "unknown") or _looks_like_raw_id(item)):
                        item["author"] = html.escape(str(real_author))
                    if real_author_id and (not item.get("authorId") or item.get("authorId") == "unknown"):
                        item["authorId"] = str(real_author_id)

                    # write through to the persistent cache so every
                    # OTHER feed that ever shows this same video gets an
                    # instant cache hit instead of another network call
                    if video_id:
                        metadata_cache[video_id] = {
                            "viewCount": view_count,
                            "lengthSeconds": length_seconds,
                            "published": published,
                            "author": real_author,
                            "authorId": real_author_id,
                            "isLive": is_live,
                            "cachedAt": int(time.time()),
                        }
            except Exception as e:
                print("STATS ENRICH FAILED:", video_id, e, flush=True)

        # last resort: if we still don't have a readable name but do have
        # a real channel ID, resolve it directly via the channel endpoint
        try:
            author = item.get("author") or ""
            author_id = item.get("authorId") or "unknown"
            looks_like_raw_id = author.startswith("UC") and len(author) > 15
            print(
                "NAME FALLBACK CHECK:", item.get("videoId"),
                "author=", repr(author), "authorId=", author_id,
                "looks_like_raw_id=", looks_like_raw_id,
                flush=True
            )
            if author_id != "unknown" and (not author or author in ("Unknown", "unknown") or looks_like_raw_id):
                real_name = get_channel_name_from_id(author_id)
                if real_name:
                    item["author"] = html.escape(str(real_name))
                    print("NAME FALLBACK APPLIED:", item.get("videoId"), "->", real_name, flush=True)
        except Exception as e:
            print("NAME FALLBACK FAILED:", item.get("videoId"), e, flush=True)

        return item

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_fetch, item) for item in to_fetch]
        for future in as_completed(futures):
            future.result()

    save_metadata_cache()

    return items

def get_relevant_videos(video_id):
    try:
        r = requests.get(
            f"{config.URL}/api/v1/videos/{video_id}",
            timeout=10
        )

        data = r.json()

        if "recommendedVideos" in data:
            results = []

            for vid in data.get("recommendedVideos", []):
                item = normalize_video(vid)
                if item:
                    results.append(item)

            results = enrich_view_counts(results)

            if results:
                return results[:15]

    except Exception as e:
        print("INVIDIOUS RELATED FAILED:", e)

    return []

import os, json, time, threading, subprocess

METADATA_CACHE_FILE = "metadata_cache.json"
metadata_cache = {}

if os.path.exists(METADATA_CACHE_FILE):
    try:
        with open(METADATA_CACHE_FILE, "r", encoding="utf-8") as f:
            metadata_cache = json.load(f)
    except:
        metadata_cache = {}

def save_metadata_cache():
    with open(METADATA_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(metadata_cache, f)

def fetch_ytdlp_metadata(video_id):
    print("STARTING METADATA FETCH:", video_id)

    if video_id in metadata_cache:
        return

    try:
        clients = [
            [],
            ["--extractor-args", "youtube:player_client=android"]
        ]

        info = None

        for extra_args in clients:
            result = subprocess.run(
                [
                    "yt-dlp",
                    *extra_args,
                    "--dump-json",
                    "--no-playlist",
                    "--no-warnings",
                    f"https://www.youtube.com/watch?v={video_id}"
                ],
                capture_output=True,
                text=True,
                timeout=30
            )

            print("METADATA CLIENT:", extra_args or "default")
            print("YT-DLP METADATA RETURN CODE:", result.returncode)
            print("YT-DLP METADATA STDOUT:", result.stdout[:200])
            print("YT-DLP METADATA STDERR:", result.stderr[:200])

            if result.returncode == 0 and result.stdout.strip():
                info = json.loads(result.stdout.splitlines()[0])
                break

        if not info:
            print("METADATA FAILED FOR:", video_id)
            return

        print("SAVING METADATA:", video_id)

        metadata_cache[video_id] = {
            "viewCount": int(info.get("view_count") or 0),
            "lengthSeconds": int(info.get("duration") or 0),
            "published": int(info.get("timestamp") or 0),
            "description": info.get("description") or "",
            "cachedAt": int(time.time())
        }

        save_metadata_cache()

        print("METADATA SAVED:", video_id)

    except Exception as e:
        print("METADATA CACHE ERROR:", e)

def apply_cached_metadata(item):
    video_id = item.get("videoId")

    if not video_id:
        return item

    cached = metadata_cache.get(video_id)

    if cached:
        print("METADATA CACHE HIT:", video_id)
        item["viewCount"] = cached.get("viewCount", item.get("viewCount", 0))
        item["lengthSeconds"] = cached.get("lengthSeconds", item.get("lengthSeconds", 0))
        item["published"] = cached.get("published", item.get("published", 0))
        item["description"] = cached.get("description", item.get("description", ""))

    else:
        print("FETCHING METADATA:", video_id)
        
        ##threading.Thread(
        ##    target=fetch_ytdlp_metadata,
        ##    args=(video_id,),
        ##    daemon=True
        ##).start()

    return item

def build_empty_history_notice():
    """Shown in place of a genuinely empty History list, so the app
    displays something explaining why instead of just a blank screen —
    same pattern as build_login_prompt."""
    return {
        "title": "No watch history yet",
        "videoId": "empty_history_notice",
        "author": "Server",
        "authorId": "unknown",
        "viewCount": 0,
        "lengthSeconds": 0,
        "published": int(time.time()),
        "description": "Videos you watch will show up here once you've watched a few."
    }

def build_login_prompt(req):
    device_id = extract_device_id(req)
    linked = is_device_linked(device_id) or any_session_linked()
    print("LOGIN PROMPT CHECK device_id:", device_id, "linked:", linked, flush=True)
    if linked:
        return None

    link_url = f"{req.url_root.rstrip('/')}/o/oauth2/programmatic_auth?device_id={device_id or 'unknown'}"

    return {
        "title": html.escape(f"Login here: {link_url}"),
        "videoId": "login_prompt",
        "author": "Server",
        "authorId": "unknown",
        "viewCount": 0,
        "lengthSeconds": 0,
        "published": int(time.time()),
        "description": "Go to this URL on any device to create a login and link this one."
    }

def primary_channel_name(name):
    """For collab/compound credits like 'Warner Bros. and IMAX', just
    use the first credited channel ('Warner Bros.') — a compound credit
    isn't a real channel, so showing/resolving the whole string is more
    confusing than useful. Splits on the same separators as
    resolve_channel_id_for_credit so display and resolution agree."""
    if not name:
        return name
    parts = re.split(r"\s+(?:and|&|x|with)\s+|,\s*", name, flags=re.IGNORECASE)
    parts = [p.strip() for p in parts if p.strip()]
    return parts[0] if parts else name

def space_safe_channel_name(name):
    """Derives a handle-style identifier from a channel's display name,
    e.g. 'Warner Bros. and IMAX' -> '@WarnerBros.' (primary channel
    only — see primary_channel_name).

    Used everywhere a channel name is shown to the app, since the app
    appears to build its 'More Videos' (uploader uploads) request
    directly from whatever name is displayed, rather than using the
    authorId we provide (confirmed via CHANNEL ROUTE HIT logs showing
    the raw name used as the URL path segment) — and that request never
    goes out at all when the name contains a literal space.

    Previously tried substituting a non-breaking space (no change) and
    literal '%20' text (worked, but displayed as ugly encoding garbage
    like 'Warner%20Bros.%20and%20IMAX'). A derived handle sidesteps the
    problem entirely — it never contains a space to begin with, so
    there's nothing for the app's URL-building to trip on, while still
    reading as a normal, clean-looking channel identifier."""
    name = primary_channel_name(name)
    if not name:
        return "Unknown"
    cleaned = re.sub(r"[^A-Za-z0-9_.\-]", "", name)
    if not cleaned:
        cleaned = "channel"
    return "@" + cleaned

def normalize_video(vid):
    title = vid.get("title") or "Untitled"
    video_id = vid.get("videoId") or vid.get("id")

    if not video_id:
        return None

    if title.strip().lower() in [
        "[deleted video]",
        "[private video]",
        "deleted video",
        "private video"
    ]:
        return None

    raw_author = str(
        vid.get("author")
        or vid.get("uploader")
        or vid.get("channel")
        or "Unknown"
    )
    author_id = str(
        vid.get("authorId")
        or vid.get("uploader_id")
        or vid.get("channel_id")
        or get_channel_id_from_name(vid.get("author") or vid.get("uploader") or vid.get("channel"))
        or "unknown"
    )

    # Some sources (yt-dlp, certain Invidious responses) hand back the raw
    # "UC..." channel ID as the author string instead of the readable name.
    # Resolve it to a real name here so it never reaches a template.
    if raw_author.startswith("UC") and len(raw_author) > 15:
        resolved_name = get_channel_name_from_id(author_id if author_id != "unknown" else raw_author)
        if resolved_name:
            raw_author = resolved_name

    raw_description = vid.get("description")
    final_description = html.escape(str(raw_description or "")[:1000])

    return {
        "title": html.escape(str(title)),
        "videoId": str(video_id),
        "author": html.escape(space_safe_channel_name(raw_author)),
        "author_urlsafe": html.escape(space_safe_channel_name(raw_author)),
        "authorId": author_id,
        "viewCount": safe_int(
            vid.get("viewCount")
            or vid.get("view_count")
            or vid.get("views")
        ),
        "lengthSeconds": int(vid.get("lengthSeconds") or vid.get("duration") or 0),
        "published": safe_published(
            vid.get("published") or vid.get("timestamp") or vid.get("publishedText")
        ),
        "description": final_description
    }

    print("RELATED VIDEO KEYS:", vid.keys())

@video.route("/feeds/api/users/<channel>/uploads")
def channel_uploads(channel):
    print("CHANNEL ROUTE HIT:", channel, flush=True)

    try:
        if channel == "default":
            device_id = extract_device_id(request)
            print("CHANNEL_UPLOADS extracted device_id:", device_id, flush=True)
            access_token = get_valid_access_token(device_id)
            print("CHANNEL_UPLOADS access_token present:", bool(access_token), flush=True)
            resolved_own_channel = get_logged_in_channel_id(access_token) if access_token else None
            print("CHANNEL_UPLOADS resolved_own_channel:", resolved_own_channel, flush=True)
            if resolved_own_channel:
                channel = resolved_own_channel
            else:
                return get.template("uploads.jinja2", {
                    "data": [],
                    "unix": get.unix,
                    "url": request.url_root.rstrip("/"),
                    "continuation": None,
                })

        if not channel.startswith("UC"):
            lookup_name = channel[1:] if channel.startswith("@") else channel

            if lookup_name.lower() in ("unknown", "channel", "unknownchannel"):
                print("CHANNEL_UPLOADS: placeholder name, not attempting resolution:", lookup_name, flush=True)
                return get.error()

            resolved = resolve_channel_id_for_credit(lookup_name)

            if (not resolved or resolved == "unknown") and channel.startswith("@"):
                # Our derived handle strips spaces entirely (e.g.
                # "Warner Bros. and IMAX" -> "@WarnerBros.andIMAX"), so
                # there's no word-boundary info left for the usual
                # search-by-name lookup. Best-effort recovery: split back
                # apart at lowercase->uppercase transitions and retry.
                spaced_guess = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", lookup_name)
                if spaced_guess != lookup_name:
                    resolved = resolve_channel_id_for_credit(spaced_guess)

            if resolved and resolved != "unknown":
                channel = resolved
            else:
                return get.error()

        # One lookup for the whole page — safe to do here (unlike in
        # normalize_video/high-traffic multi-channel feeds) since every
        # video on this page shares the same channel, so it's a single
        # cached network call, not one per video.
        real_handle = get_real_channel_handle(channel)

        # honor GData's numeric start-index/max-results pagination —
        # computed up front now so we know how many videos we actually
        # need before deciding whether to fetch further pages.
        try:
            start_index = max(int(request.args.get("start-index", 1)), 1)
        except (TypeError, ValueError):
            start_index = 1
        try:
            max_results = max(int(request.args.get("max-results", 25)), 1)
        except (TypeError, ValueError):
            max_results = 25

        needed = start_index - 1 + max_results

        # Previously this only ever fetched a single page from Invidious
        # and sliced locally — meaning "page 2" of a channel with more
        # uploads than fit in one Invidious call always silently came
        # back empty, telling the app to stop even though more videos
        # genuinely existed. Now follows Invidious's own continuation
        # token, fetching further pages until there's enough to cover
        # the requested window (capped to avoid unbounded fetching on
        # channels with huge upload counts).
        videos = []
        continuation = None
        MAX_PAGE_FETCHES = 10
        for _ in range(MAX_PAGE_FETCHES):
            page_url = f"{config.URL}/api/v1/channels/{channel}/videos?sort_by=newest"
            if continuation:
                page_url += f"&continuation={continuation}"
            data = get.fetch(page_url)
            if not data:
                break
            batch = data.get("videos", [])
            if not batch:
                break
            videos.extend(batch)
            continuation = data.get("continuation")
            if len(videos) >= needed or not continuation:
                break

        print(
            "CHANNEL_UPLOADS regular videos fetched:", len(videos),
            "| needed for this window:", needed,
            "| more available:", bool(continuation),
            flush=True
        )

        if not videos and not continuation:
            return get.error()

        # Invidious keeps Shorts on a completely separate endpoint from
        # regular uploads (mirrors YouTube's own separate Shorts tab) —
        # a plain /videos fetch silently excludes them entirely. Merge
        # them in and re-sort by published date so they're properly
        # interleaved with regular uploads by recency, not just
        # tacked on at the end.
        shorts_data = get.fetch(
            f"{config.URL}/api/v1/channels/{channel}/shorts?sort_by=newest"
        )
        if shorts_data:
            shorts_videos = shorts_data.get("videos", [])
            print("CHANNEL_UPLOADS shorts found:", len(shorts_videos), flush=True)
            for sv in shorts_videos:
                sv["_is_short"] = True

            # Invidious's /shorts listing is a lightweight endpoint that
            # commonly comes back with missing/wrong duration and
            # inaccurate published dates for each short — this pulls the
            # real per-video data (same mechanism already used elsewhere,
            # with its own caching) scoped to just this channel's shorts,
            # not the whole app, so it doesn't repeat the earlier
            # across-the-board performance issue.
            shorts_videos = enrich_view_counts(shorts_videos, max_enrich=100, force_refresh=True)

            # last-resort placeholder for anything still missing a
            # duration even after the real per-video fetch above
            for sv in shorts_videos:
                if not sv.get("lengthSeconds"):
                    sv["lengthSeconds"] = 30

            # A pure chronological merge+sort lets whichever type this
            # channel posts more/more-recently-of dominate entirely —
            # confirmed: 108 merged items, 0 shorts made it into the
            # first 25 because this channel's regular uploads all
            # happened to be newer. Interleave instead (roughly 2
            # regular videos per short) so both types are guaranteed to
            # actually show up on the first page.
            videos.sort(key=lambda v: v.get("published") or 0, reverse=True)
            shorts_videos.sort(key=lambda v: v.get("published") or 0, reverse=True)
            merged = []
            vi, si = 0, 0
            while vi < len(videos) or si < len(shorts_videos):
                for _ in range(2):
                    if vi < len(videos):
                        merged.append(videos[vi])
                        vi += 1
                if si < len(shorts_videos):
                    merged.append(shorts_videos[si])
                    si += 1
            videos = merged

        shorts_in_window = sum(1 for v in videos[start_index - 1: start_index - 1 + max_results] if v.get("_is_short"))
        print(
            "CHANNEL_UPLOADS pagination window:", start_index, "to", start_index - 1 + max_results,
            "| shorts in this window:", shorts_in_window,
            "| total merged pool:", len(videos),
            flush=True
        )

        has_more = len(videos) > needed or bool(continuation)
        videos = videos[start_index - 1: start_index - 1 + max_results]

        clean = []
        shorts_survived = 0

        for vid in videos:
            if vid.get("type") == "parse-error":
                continue

            item = normalize_video(vid)

            if item:
                if vid.get("_is_short"):
                    shorts_survived += 1
                item["viewCount"] = safe_int(
                    vid.get("viewCount")
                    or vid.get("view_count")
                    or vid.get("views")
                    or item.get("viewCount")
                )

                item["lengthSeconds"] = safe_int(
                    vid.get("lengthSeconds")
                    or vid.get("length_seconds")
                    or vid.get("duration")
                    or item.get("lengthSeconds")
                )

                item["published"] = safe_published(
                    vid.get("published")
                    or vid.get("publishedText")
                    or vid.get("timestamp")
                    or item.get("published")
                )
                clean.append(item)

        if real_handle:
            print("CHANNEL_UPLOADS applying real handle to all items:", real_handle, flush=True)
            for item in clean:
                item["author"] = html.escape(real_handle)
                item["author_urlsafe"] = html.escape(real_handle)

        print(
            "CHANNEL_UPLOADS final render:", len(clean), "total items,",
            shorts_survived, "of which are shorts", flush=True
        )

        return get.template("uploads.jinja2", {
            "data": clean,
            "unix": get.unix,
            "url": request.url_root.rstrip("/"),
            "continuation": (str(start_index + max_results) if has_more else None),
            "channel_id": channel,
        })

    except Exception as e:
        print("CHANNEL UPLOADS ERROR:", e, flush=True)
        return get.error()

@video.route("/feeds/api/users/<channel>/subscriptions")
def subscriptions_list(channel):
    """Real Subscriptions list — the channels you're subscribed to, via
    YouTube Data API v3's subscriptions.list. This used to route through
    channel_uploads(), which showed a video feed instead of an actual
    subscriptions list — meaning there was never any way for the app to
    tell 'am I subscribed to X', since it wasn't looking at subscription
    data at all. Each entry includes its real subscription ID so the
    app's unsubscribe (DELETE) action has something valid to reference."""
    device_id = extract_device_id(request)
    access_token = get_valid_access_token(device_id)

    url = request.url_root.rstrip("/")
    empty = get.template("subscriptions.jinja2", {
        "data": [], "unix": get.unix, "url": url, "channel_id": channel,
    })

    if not access_token:
        return empty

    try:
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/subscriptions",
            params={"part": "snippet", "mine": "true", "maxResults": 50},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        print("SUBSCRIPTIONS LIST status:", r.status_code, flush=True)
        if not r.ok:
            print("SUBSCRIPTIONS LIST body:", r.text[:400], flush=True)
            return empty
        items = r.json().get("items", [])
    except Exception as e:
        print("SUBSCRIPTIONS LIST ERROR:", repr(e), flush=True)
        return empty

    clean = []
    for it in items:
        snippet = it.get("snippet") or {}
        sub_id = it.get("id")
        channel_id = (snippet.get("resourceId") or {}).get("channelId") or "unknown"
        thumbs = snippet.get("thumbnails") or {}
        thumb_url = (
            (thumbs.get("high") or {}).get("url")
            or (thumbs.get("default") or {}).get("url")
            or ""
        )
        clean.append({
            "title": html.escape(snippet.get("title") or "Untitled"),
            "subscriptionId": sub_id,
            "channelId": channel_id,
            "author": html.escape(space_safe_channel_name(snippet.get("title") or "Unknown")),
            "authorId": channel_id,
            "thumbnail": thumb_url,
            "description": html.escape((snippet.get("description") or "")[:500]),
            "published": safe_published(snippet.get("publishedAt")),
        })

    try:
        start_index = max(int(request.args.get("start-index", 1)), 1)
    except (TypeError, ValueError):
        start_index = 1
    try:
        max_results = max(int(request.args.get("max-results", 25)), 1)
    except (TypeError, ValueError):
        max_results = 25

    print(
        "SUBSCRIPTIONS_LIST window requested:", start_index, "to", start_index - 1 + max_results,
        "| total fetched:", len(clean),
        flush=True
    )
    clean = clean[start_index - 1: start_index - 1 + max_results]

    return get.template("subscriptions.jinja2", {
        "data": clean,
        "unix": get.unix,
        "url": url,
        "channel_id": channel,
    })

@video.route("/feeds/api/users/<channel>/subscriptions/<subscription_id>", methods=["GET"])
def subscription_entry(channel, subscription_id):
    """Tapping a subscription in the list may hit this entry's own
    <link rel='self'>/<link rel='edit'> URL rather than following the
    gd:feedLink to the channel's /uploads feed. Resolve the subscription
    ID straight to its channel via the Data API's id= filter (cheaper
    than re-listing every subscription) and just serve that channel's
    uploads feed here directly, so either way of navigating in works."""
    device_id = extract_device_id(request)
    access_token = get_valid_access_token(device_id)
    if not access_token:
        return get.error()

    try:
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/subscriptions",
            params={"part": "snippet", "id": subscription_id},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        print("SUBSCRIPTION ENTRY status:", r.status_code, flush=True)
        items = r.json().get("items", []) if r.ok else []
        if not items:
            print("SUBSCRIPTION ENTRY: no matching subscription for", subscription_id, flush=True)
            return get.error()
        channel_id = items[0].get("snippet", {}).get("resourceId", {}).get("channelId")
        if not channel_id:
            return get.error()
    except Exception as e:
        print("SUBSCRIPTION ENTRY ERROR:", repr(e), flush=True)
        return get.error()

    return channel_uploads(channel_id)

@video.route("/feeds/api/users/<channel>/subscriptions/<subscription_id>", methods=["DELETE"])
def unsubscribe(channel, subscription_id):
    """Unsubscribe, backed by YouTube Data API v3's subscriptions.delete.
    Confirmed against a real capture — the app DELETEs using the real
    subscription ID it got back from subscribe_to_channel()'s response."""
    device_id = extract_device_id(request)
    access_token = get_valid_access_token(device_id)
    if not access_token:
        return get.error()

    try:
        r = requests.delete(
            "https://www.googleapis.com/youtube/v3/subscriptions",
            params={"id": subscription_id},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        print("UNSUBSCRIBE status:", r.status_code, flush=True)
        if r.status_code not in (200, 204):
            print("UNSUBSCRIBE body:", r.text[:400], flush=True)
            return get.error()
    except Exception as e:
        print("UNSUBSCRIBE ERROR:", repr(e), flush=True)
        return get.error()

    return "", 200

@video.route("/feeds/api/users/<channel>/subscriptions", methods=["POST"])
def subscribe_to_channel(channel):
    """Real subscribe action, backed by YouTube Data API v3's
    subscriptions.insert. The classic app POSTs an XML body like:
        <entry ...><yt:username>CHANNEL_ID</yt:username></entry>
    (note: despite the tag name, this app puts a real channel ID here,
    not a username string — confirmed from a live capture)."""
    device_id = extract_device_id(request)
    access_token = get_valid_access_token(device_id)
    if not access_token:
        return get.error()

    try:
        body = request.get_data(as_text=True)
        root = ET.fromstring(body)
    except Exception as e:
        print("SUBSCRIBE PARSE ERROR:", e, flush=True)
        return get.error()

    ns = {"yt": "http://gdata.youtube.com/schemas/2007"}
    username_el = root.find("yt:username", ns)
    target_channel_id = username_el.text.strip() if username_el is not None and username_el.text else None

    if not target_channel_id:
        print("SUBSCRIBE: no channel id found in body", flush=True)
        return get.error()

    try:
        r = requests.post(
            "https://www.googleapis.com/youtube/v3/subscriptions",
            params={"part": "snippet"},
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"snippet": {"resourceId": {"kind": "youtube#channel", "channelId": target_channel_id}}},
            timeout=10,
        )
        print("SUBSCRIBE status:", r.status_code, flush=True)
        if not r.ok:
            print("SUBSCRIBE body:", r.text[:400], flush=True)
            return get.error()
        new_sub = r.json()
    except Exception as e:
        print("SUBSCRIBE ERROR:", repr(e), flush=True)
        return get.error()

    sub_id = new_sub.get("id", target_channel_id)
    published = new_sub.get("snippet", {}).get("publishedAt") or "2026-01-01T00:00:00.000Z"
    url_root = request.url_root.rstrip("/")

    return Response(
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<entry xmlns='http://www.w3.org/2005/Atom' xmlns:yt='http://gdata.youtube.com/schemas/2007' "
        "xmlns:gd='http://schemas.google.com/g/2005'>"
        f"<id>tag:youtube.com,2008:subscription:{sub_id}</id>"
        f"<published>{published}</published>"
        f"<updated>{published}</updated>"
        "<category scheme='http://schemas.google.com/g/2005#kind' term='http://gdata.youtube.com/schemas/2007#subscription'/>"
        "<category scheme='http://gdata.youtube.com/schemas/2007/subscriptiontypes.cat' term='channel'/>"
        f"<link rel='self' type='application/atom+xml' href='{url_root}/feeds/api/users/default/subscriptions/{sub_id}'/>"
        f"<link rel='edit' type='application/atom+xml' href='{url_root}/feeds/api/users/default/subscriptions/{sub_id}'/>"
        f"<yt:username>{target_channel_id}</yt:username>"
        "</entry>",
        mimetype="application/atom+xml"
    )

@video.route("/feeds/api/users/<channel>/favorites")
def favorites(channel):
    """Favorites — backed by the real YouTube Liked Videos playlist
    ('LL' is a reserved special playlist ID that resolves to 'your
    liked videos' for whichever account the access token belongs to).
    This used to reuse channel_uploads()'s logic, which made it show
    the exact same list as Uploads and appears to have triggered a
    broken 'merge accounts' prompt in the app — hence its own route."""
    device_id = extract_device_id(request)
    access_token = get_valid_access_token(device_id)

    url = request.url_root.rstrip("/")
    empty = get.template("uploads.jinja2", {
        "data": [], "unix": get.unix, "url": url, "continuation": None,
    })

    if not access_token:
        return empty

    try:
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/playlistItems",
            params={"part": "snippet", "playlistId": "LL", "maxResults": 25},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        print("FAVORITES (LL) status:", r.status_code, flush=True)
        if not r.ok:
            print("FAVORITES (LL) body:", r.text[:400], flush=True)
            return empty
        items = r.json().get("items", [])
    except Exception as e:
        print("FAVORITES ERROR:", repr(e), flush=True)
        return empty

    clean = []
    for it in items:
        snippet = it.get("snippet", {})
        vid_id = snippet.get("resourceId", {}).get("videoId")
        if not vid_id:
            continue
        clean.append({
            "title": html.escape(snippet.get("title", "Untitled")),
            "videoId": vid_id,
            "author": html.escape(space_safe_channel_name(snippet.get("videoOwnerChannelTitle", "Unknown"))),
            "authorId": snippet.get("videoOwnerChannelId", "unknown"),
            "viewCount": 0,
            "lengthSeconds": 0,
            "published": 0,
            "description": "",
        })

    clean = enrich_view_counts(clean)

    return get.template("uploads.jinja2", {
        "data": clean,
        "unix": get.unix,
        "url": url,
        "continuation": None,
    })

@video.route("/feeds/api/users/<channel>/favorites", methods=["POST"])
def add_favorite(channel):
    """Add-to-favorites, backed by YouTube Data API v3's videos.rate
    (liking a video also adds it to the Liked Videos / 'LL' playlist,
    which is what our Favorites GET route already reads from). UNVERIFIED
    against a real capture — built against the documented historical
    GData v2 convention (POST body containing
    <id>tag:youtube.com,2008:video:VIDEO_ID</id>). Test this and send
    the actual request if it doesn't work."""
    device_id = extract_device_id(request)
    access_token = get_valid_access_token(device_id)
    if not access_token:
        return get.error()

    try:
        body = request.get_data(as_text=True)
        root = ET.fromstring(body)
    except Exception as e:
        print("ADD_FAVORITE PARSE ERROR:", e, flush=True)
        return get.error()

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    id_el = root.find("atom:id", ns)
    if id_el is None:
        id_el = root.find("id")  # some GData clients omit the namespace prefix

    if id_el is None or not id_el.text:
        print("ADD_FAVORITE: no video id found in body", flush=True)
        return get.error()

    video_id = id_el.text.strip().rsplit(":", 1)[-1]

    try:
        r = requests.post(
            "https://www.googleapis.com/youtube/v3/videos/rate",
            params={"id": video_id, "rating": "like"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        print("ADD_FAVORITE status:", r.status_code, flush=True)
        if not r.ok:
            print("ADD_FAVORITE body:", r.text[:400], flush=True)
            return get.error()
    except Exception as e:
        print("ADD_FAVORITE ERROR:", repr(e), flush=True)
        return get.error()

    return Response(
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<entry xmlns='http://www.w3.org/2005/Atom' xmlns:yt='http://gdata.youtube.com/schemas/2007'>"
        f"<id>tag:youtube.com,2008:video:{video_id}</id>"
        "</entry>",
        mimetype="application/atom+xml"
    )

@video.route("/feeds/api/users/<channel>/playlists", methods=["POST"])
def create_playlist(channel):
    """Create-new-playlist, backed by YouTube Data API v3's
    playlists.insert. UNVERIFIED against a real capture — built against
    the documented historical GData v2 convention (POST body containing
    <title> and optionally <summary>). Test this and send the actual
    request if it doesn't work."""
    device_id = extract_device_id(request)
    access_token = get_valid_access_token(device_id)
    if not access_token:
        return get.error()

    try:
        body = request.get_data(as_text=True)
        root = ET.fromstring(body)
    except Exception as e:
        print("CREATE_PLAYLIST PARSE ERROR:", e, flush=True)
        return get.error()

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    title_el = root.find("atom:title", ns)
    summary_el = root.find("atom:summary", ns)

    title = title_el.text.strip() if title_el is not None and title_el.text else "New Playlist"
    description = summary_el.text.strip() if summary_el is not None and summary_el.text else ""

    try:
        r = requests.post(
            "https://www.googleapis.com/youtube/v3/playlists",
            params={"part": "snippet,status"},
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={
                "snippet": {"title": title, "description": description},
                "status": {"privacyStatus": "private"},
            },
            timeout=10,
        )
        print("CREATE_PLAYLIST status:", r.status_code, flush=True)
        if not r.ok:
            print("CREATE_PLAYLIST body:", r.text[:400], flush=True)
            return get.error()
        new_playlist = r.json()
    except Exception as e:
        print("CREATE_PLAYLIST ERROR:", repr(e), flush=True)
        return get.error()

    playlist_id = new_playlist.get("id", "")
    published = new_playlist.get("snippet", {}).get("publishedAt") or "2026-01-01T00:00:00.000Z"
    url_root = request.url_root.rstrip("/")

    return Response(
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<entry xmlns='http://www.w3.org/2005/Atom' xmlns:yt='http://gdata.youtube.com/schemas/2007' "
        "xmlns:gd='http://schemas.google.com/g/2005'>"
        f"<id>tag:youtube.com,2008:playlist:{playlist_id}</id>"
        f"<published>{published}</published>"
        f"<updated>{published}</updated>"
        "<category scheme='http://schemas.google.com/g/2005#kind' term='http://gdata.youtube.com/schemas/2007#playlistLink'/>"
        f"<title type='text'>{html.escape(title)}</title>"
        f"<summary type='text'>{html.escape(description)}</summary>"
        f"<link rel='self' type='application/atom+xml' href='{url_root}/feeds/api/playlists/{playlist_id}'/>"
        f"<link rel='edit' type='application/atom+xml' href='{url_root}/feeds/api/playlists/{playlist_id}'/>"
        f"<link rel='http://gdata.youtube.com/schemas/2007#video.responses' type='application/atom+xml' href='{url_root}/feeds/api/playlists/{playlist_id}'/>"
        f"<yt:playlistId>{playlist_id}</yt:playlistId>"
        "<yt:countHint>0</yt:countHint>"
        "</entry>",
        mimetype="application/atom+xml"
    )

# real per-account feeds, backed by the linked Google account's access
# token via YouTube's internal InnerTube API (Invidious can't serve these
# since it has no idea who you are — only Google does)
PERSONAL_BROWSE_IDS = {
    "newsubscriptionvideos": "FEsubscriptions",
    "watchhistory": "FEhistory",
    "watchlater": "VLWL",
}

@video.route("/feeds/api/users/<channel>/newsubscriptionvideos")
@video.route("/feeds/api/users/<channel>/watchhistory")
@video.route("/feeds/api/users/<channel>/watchlater")
@video.route("/feeds/api/users/<channel>/recommendations")
def personalized_feed_stub(channel):
    feed_name = request.path.rstrip("/").split("/")[-1]
    print("PERSONALIZED FEED HIT:", feed_name, flush=True)

    data = []
    browse_id = PERSONAL_BROWSE_IDS.get(feed_name)

    if browse_id:
        device_id = extract_device_id(request)
        access_token = get_valid_access_token(device_id)
        if access_token:
            try:
                # Previously bumped to 60 for newsubscriptionvideos so more
                # subscribed channels would have a chance of being
                # represented — but a larger fetched pool combined with
                # not respecting max-results is a likely crash contributor
                # on older clients. Back to 15 for a smaller, safer payload.
                fetch_limit = 15
                data = fetch_personal_feed(access_token, browse_id, limit=fetch_limit)
                # deliberately NOT running this through enrich_view_counts —
                # that meant up to 60 extra per-video Invidious calls on
                # every load of this feed, which is likely what was making
                # it slow/crash-prone. YouTube's own internal feed response
                # already includes viewCount/author/authorId directly.
            except Exception as e:
                print("PERSONALIZED FEED ERROR:", feed_name, e, flush=True)
                data = []

    try:
        start_index = max(int(request.args.get("start-index", 1)), 1)
    except (TypeError, ValueError):
        start_index = 1
    try:
        max_results = max(int(request.args.get("max-results", 25)), 1)
    except (TypeError, ValueError):
        max_results = 25

    print(
        "PERSONALIZED FEED window requested:", start_index, "to", start_index - 1 + max_results,
        "| total fetched:", len(data),
        flush=True
    )
    data = data[start_index - 1: start_index - 1 + max_results]

    return get.template("uploads.jinja2", {
        "data": data,
        "unix": get.unix,
        "url": request.url_root.rstrip("/"),
        "continuation": None,
    })

def get_playlist_from_invidious(playlist_id):
    url = f"{config.URL}/api/v1/playlists/{playlist_id}"

    try:
        r = requests.get(url, timeout=10, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json"
        })

        print("PLAYLIST URL:", url)
        print("STATUS:", r.status_code)
        print("TEXT:", r.text[:500])

        if not r.text.strip():
            raise Exception("Invidious returned blank playlist")

        playlist = r.json()
        videos = playlist.get("videos", [])

        # normalize_video() resolves missing authorId via a real network
        # search per unique channel name — doing that serially for ~30
        # videos (most uncached on a fresh load) is what was making
        # Featured slow to load. Resolve every unique name concurrently
        # first so the cache is already warm by the time the loop below
        # runs, instead of blocking on one search at a time.
        unique_names = {
            (vid.get("author") or vid.get("uploader") or vid.get("channel"))
            for vid in videos
            if not (vid.get("authorId") or vid.get("uploader_id") or vid.get("channel_id"))
            and (vid.get("author") or vid.get("uploader") or vid.get("channel"))
        }
        uncached_names = [n for n in unique_names if n not in channel_id_cache]
        if uncached_names:
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = [executor.submit(get_channel_id_from_name, n) for n in uncached_names]
                for future in as_completed(futures):
                    future.result()

        clean = []

        for vid in videos:
            item = normalize_video(vid)

            if not item:
                continue

            clean.append(item)

        return clean

    except Exception as e:
        print("INVIDIOUS PLAYLIST FAILED:", e)
        return []

# InviData (github.com/StealthTheAngryBird/InviData) — a similar
# GData-translation project — uses Invidious's SEARCH endpoint with a
# generic query for Featured/Top Rated/etc, taking author/authorId
# directly from search results with just a simple fallback, no
# enrichment logic at all. That only works if search results are
# reliably complete — which they appear to be, unlike the curated
# PLAYLIST endpoint we were using, which seems to have much less
# reliable author/authorId completeness. This is a structural fix
# rather than another enrichment patch.
FEED_SEARCH_QUERIES = {
    "top_rated": "top rated videos",
    "most_viewed": "most viewed videos",
    "recently_featured": "trending videos",
    "most_popular": "popular videos",
    "most_discussed": "most discussed videos",
    "on_the_web": "viral videos",
}

def get_videos_via_search(query, limit=15, sort_by=None, date=None):
    url = f"{config.URL}/api/v1/search?q={query}&type=video"
    if sort_by:
        url += f"&sort_by={sort_by}"
    if date:
        url += f"&date={date}"
    data = get.fetch(url)
    if not data:
        return []

    clean = []
    seen = set()

    for item in data:
        if item.get("type") != "video":
            continue
        vid_id = item.get("videoId")
        if not vid_id or vid_id in seen:
            continue
        if item.get("liveNow") or item.get("isLive") or item.get("isUpcoming"):
            continue
        seen.add(vid_id)

        author_name = item.get("author") or "YouTube"
        author_id = item.get("authorId")
        if not author_id:
            author_id = resolve_channel_id_for_credit(author_name)

        clean.append({
            "title": html.escape(item.get("title") or "Untitled"),
            "videoId": vid_id,
            "author": html.escape(space_safe_channel_name(author_name)),
            "authorId": author_id or "unknown",
            "viewCount": int(item.get("viewCount") or 0),
            "lengthSeconds": int(item.get("lengthSeconds") or 0),
            "published": int(item.get("published") or 0),
            "description": html.escape((item.get("description") or "")[:1000]),
        })

        if len(clean) >= limit:
            break

    random.shuffle(clean)
    return clean

def get_featured_from_hourly_playlists():

    playlist_url = "https://www.youtube.com/playlist?list=PL-p0-Yh03xpi2AsCiyuafMeQrMF6czMoL"

    data = []
    seen = set()

    playlist_id = playlist_url.split("list=")[-1].split("&")[0]

    entries = get_playlist_from_invidious(playlist_id)

    if not entries:
        print("INVIDIOUS FAILED, FALLING BACK TO FALLBACK PLAYLIST")

        fallback_playlist_url = "https://www.youtube.com/playlist?list=PLMdDlbZ4P5Kl63ywRrbyVljrU4pA8YQ8Z"
        fallback_playlist_id = fallback_playlist_url.split("list=")[-1].split("&")[0]

        entries = get_playlist_from_invidious(fallback_playlist_id)

    random.shuffle(entries)

    for vid in entries:

        vid_id = vid.get("videoId") or vid.get("id")
        title = vid.get("title")

        if not vid_id or not title:
            continue

        if vid_id in seen:
            continue

        if title.strip().lower() in [
            "[deleted video]",
            "[private video]",
            "deleted video",
            "private video"
        ]:
            continue

        seen.add(vid_id)

        view_count = int(vid.get("viewCount") or vid.get("view_count") or 0)

        item = {
            "title": html.escape(title),
            "videoId": vid_id,
            "author": html.escape(space_safe_channel_name(
                vid.get("author")
                or vid.get("uploader")
                or vid.get("channel")
                or "Unknown"
            )),
            "authorId": (
                vid.get("authorId")
                or vid.get("uploader_id")
                or vid.get("channel_id")
                or "unknown"
            ),
            "viewCount": view_count,
            "lengthSeconds": int(vid.get("lengthSeconds") or vid.get("duration") or 0),
            "published": int(vid.get("published") or vid.get("timestamp") or 0),
            "description": html.escape((vid.get("description") or "")[:1000])
        }

        item["authorId"] = (
            item.get("authorId")
            if item.get("authorId") not in ["unknown", "Unknown", "", None]
            else resolve_channel_id_for_credit(item.get("author"))
        )

        print(
            "UPLOADER DEBUG:",
            item.get("author"),
            item.get("authorId"),
            flush=True
        )

        print("UPLOADER ID:", item["author"], item["authorId"], flush=True)

        item = apply_cached_metadata(item)
        data.append(item)

        if len(data) >= 15:
            random.shuffle(data)
            return enrich_view_counts(data, force_refresh=True)

    random.shuffle(data)
    return enrich_view_counts(data, force_refresh=True)

channel_id_cache = {}
channel_name_cache = {}
channel_handle_cache = {}

def get_real_channel_handle(channel_id):
    """Attempts to get the channel's actual registered @handle (e.g.
    '@memeguyonyt') rather than a derived one, via a live scrape of
    YouTube's own channel page header — Invidious's API doesn't expose
    real handles at all (authorUrl only ever gives /channel/UC...).

    This is inherently more fragile than an API field (breaks if
    YouTube changes their page markup, same risk the existing
    subscriber-count scrape already carries) and is a real network
    call, so it's cached per channel_id and any failure just returns
    None so callers can fall back to the derived handle instead of
    erroring."""
    if not channel_id or not channel_id.startswith("UC"):
        return None
    if channel_id in channel_handle_cache:
        return channel_handle_cache[channel_id]
    try:
        info = yt.metadata.simple_channel_info(channel_id)
        handle = info.get("handle")
        channel_handle_cache[channel_id] = handle
        print("GET_REAL_CHANNEL_HANDLE:", channel_id, "->", handle, flush=True)
        return handle
    except Exception as e:
        print("GET_REAL_CHANNEL_HANDLE ERROR:", channel_id, repr(e), flush=True)
        channel_handle_cache[channel_id] = None
        return None

@video.route("/feeds/api/users/<channel>")
@video.route("/feeds/api/users/<channel>/")
def channel_profile(channel):
    """Channel PROFILE endpoint — a single entry with the channel's real
    display name, subscriber count, etc. This is architecturally
    different from the uploads/videos feed, and was missing entirely;
    this exact bare URL was previously claimed by channel_uploads(),
    which returned a full video-list feed instead of a profile entry.
    If the app resolves a channel's display name via a separate request
    to this URL (standard GData behavior, confirmed by a similar project,
    github.com/StealthTheAngryBird/InviData), that would explain why
    fixing the video-level 'author' field never mattered — it was
    getting the wrong response shape here and falling back to the ID."""
    channel_id = channel
    channel_name = channel
    sub_count = 0
    video_count = 10
    view_count = 0
    description = "YouTube Channel"

    print("CHANNEL_PROFILE requested for:", channel, flush=True)

    if channel_id in ("unknown", "Unknown", ""):
        # this means whatever built the video's link never had a real
        # channel ID to begin with (our own internal placeholder leaked
        # through into a real request) — nothing to resolve here.
        print("CHANNEL_PROFILE: got literal placeholder 'unknown', nothing to resolve", flush=True)
        channel_name = "Unknown Channel"
    else:
        if not channel_id.startswith("UC"):
            resolved = get_channel_id_from_name(channel_id)
            print("CHANNEL_PROFILE: resolved name->id:", channel_id, "->", resolved, flush=True)
            if resolved and resolved != "unknown":
                channel_id = resolved

        if channel_id.startswith("UC"):
            try:
                data = get.fetch(f"{config.URL}/api/v1/channels/{channel_id}")
                print("CHANNEL_PROFILE: fetch result for", channel_id, "->", bool(data), flush=True)
                if data and data.get("author"):
                    channel_name = data["author"]
                    sub_count = int(data.get("subCount") or 0)
                    description = data.get("description") or description
                    video_count = int(data.get("videoCount") or video_count)
                    view_count = int(data.get("totalViews") or 0)
                else:
                    print("CHANNEL_PROFILE: fetch returned no usable 'author' for", channel_id, flush=True)
            except Exception as e:
                print("CHANNEL PROFILE FETCH ERROR:", channel_id, e, flush=True)

    url = request.url_root.rstrip("/")

    return Response(
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<entry xmlns='http://www.w3.org/2005/Atom' xmlns:media='http://search.yahoo.com/mrss/' "
        "xmlns:gd='http://schemas.google.com/g/2005' xmlns:yt='http://gdata.youtube.com/schemas/2007'>"
        f"<id>{url}/feeds/api/users/{html.escape(channel_id)}</id>"
        "<published>2005-04-15T00:00:00.000Z</published>"
        "<updated>2026-01-01T00:00:00.000Z</updated>"
        "<category scheme='http://schemas.google.com/g/2005#kind' term='http://gdata.youtube.com/schemas/2007#userProfile'/>"
        f"<title type='text'>{html.escape(channel_name)}</title>"
        f"<content type='text'>{html.escape(description)}</content>"
        f"<link rel='alternate' type='text/html' href='https://www.youtube.com/channel/{html.escape(channel_id)}'/>"
        f"<link rel='self' type='application/atom+xml' href='{url}/feeds/api/users/{html.escape(channel_id)}?v=2'/>"
        "<author>"
        f"<name>{html.escape(channel_name)}</name>"
        f"<uri>{url}/feeds/api/users/{html.escape(channel_id)}</uri>"
        "</author>"
        f"<gd:feedLink rel='http://gdata.youtube.com/schemas/2007#user.uploads' href='{url}/feeds/api/users/{html.escape(channel_id)}/uploads' countHint='{video_count}'/>"
        f"<gd:feedLink rel='http://gdata.youtube.com/schemas/2007#user.playlists' href='{url}/feeds/api/users/{html.escape(channel_id)}/playlists'/>"
        f"<gd:feedLink rel='http://gdata.youtube.com/schemas/2007#user.subscriptions' href='{url}/feeds/api/users/{html.escape(channel_id)}/subscriptions' countHint='0'/>"
        f"<gd:feedLink rel='http://gdata.youtube.com/schemas/2007#user.newsubscriptionvideos' href='{url}/feeds/api/users/{html.escape(channel_id)}/newsubscriptionvideos'/>"
        "<yt:maxUploadDuration seconds='0'/>"
        f"<yt:statistics lastWebAccess='1970-01-01T00:00:00.000Z' subscriberCount='{sub_count}' videoWatchCount='0' viewCount='{view_count}' totalUploadViews='{view_count}'/>"
        f"<yt:username display='{html.escape(channel_name)}'>{html.escape(channel_id)}</yt:username>"
        "</entry>",
        mimetype="application/atom+xml"
    )

def get_channel_name_from_id(channel_id):
    """Reverse of get_channel_id_from_name — resolves a real channel name
    from an ID, for sources that only give us the raw UC... ID with no
    readable name attached."""
    if not channel_id or channel_id == "unknown":
        print("GET_CHANNEL_NAME_FROM_ID: skipped, no usable id:", channel_id, flush=True)
        return None

    if channel_id in channel_name_cache:
        print("GET_CHANNEL_NAME_FROM_ID cache hit:", channel_id, "->", channel_name_cache[channel_id], flush=True)
        return channel_name_cache[channel_id]

    try:
        r = requests.get(
            f"{config.URL}/api/v1/channels/{channel_id}",
            timeout=10,
        )
        print("GET_CHANNEL_NAME_FROM_ID status:", channel_id, r.status_code, flush=True)
        if not r.ok:
            print("GET_CHANNEL_NAME_FROM_ID body:", r.text[:300], flush=True)
            return None
        data = r.json()
        name = data.get("author")
        print("GET_CHANNEL_NAME_FROM_ID resolved name:", channel_id, "->", repr(name), flush=True)
        if name:
            channel_name_cache[channel_id] = name
            return name
        else:
            print("GET_CHANNEL_NAME_FROM_ID: response had no 'author' field, raw keys:", list(data.keys()), flush=True)
    except Exception as e:
        print("GET CHANNEL NAME FROM ID FAILED:", channel_id, e, flush=True)

    return None

def resolve_channel_id_for_credit(name):
    """Wraps get_channel_id_from_name to handle compound/collab credits
    like 'Warner Bros. and IMAX' or 'Spinnin' Records and The Second
    Voice' — these aren't real single channels, so a direct name search
    for the whole string can fail (or land on an unrelated channel via
    the 'fallback first result' behavior in get_channel_id_from_name).
    Try the primary (first-credited) channel first, matching how
    space_safe_channel_name simplifies collab credits for display —
    falls back to the full string only if that doesn't resolve."""
    if not name:
        return "unknown"

    primary = primary_channel_name(name)
    resolved = get_channel_id_from_name(primary)
    if resolved and resolved != "unknown":
        if primary != name:
            print("CREDIT SIMPLIFIED:", repr(name), "->", primary, "->", resolved, flush=True)
        return resolved

    if primary != name:
        resolved = get_channel_id_from_name(name)
        if resolved and resolved != "unknown":
            return resolved

    return "unknown"

def get_channel_id_from_name(name):

    if not name:
        return "unknown"

    if name in channel_id_cache:
        return channel_id_cache[name]

    try:
        r = requests.get(
            f"{config.URL}/api/v1/search",
            params={
                "q": name,
                "type": "channel"
            },
            timeout=10
        )

        data = r.json()

        if isinstance(data, list):

            for item in data:

                author = (
                    item.get("author")
                    or ""
                ).lower().strip()

                if author == name.lower().strip():

                    cid = item.get("authorId")

                    if cid:
                        channel_id_cache[name] = cid
                        return cid

            # fallback first result
            if data:
                cid = data[0].get("authorId")

                if cid:
                    channel_id_cache[name] = cid
                    return cid

    except Exception as e:
        print("CHANNEL ID SEARCH ERROR:", e)

    return "unknown"

print(
    "TEST CHANNEL ID:",
    get_channel_id_from_name("MrBeast")
)

# featured videos
# 2 alternate routes for popular page and search results
@video.route("/feeds/api/standardfeeds/<regioncode>/<popular>")
@video.route("/feeds/api/standardfeeds/<popular>")
@video.route("/<int:res>/feeds/api/standardfeeds/<regioncode>/<popular>")
@video.route("/<int:res>/feeds/api/standardfeeds/<popular>")
def frontpage(regioncode="US", popular=None, res=''):

    # Clamp Res
    if type(res) == int:
        res = min(max(res, 144), config.RESMAX)

    url = request.url_root + str(res) 
    # trending videos categories
    # the menu got less because of youtube removing it.
    apiurl = config.URL + "/api/v1/trending?region=" + regioncode
    if popular == "most_popular_Film":
        apiurl = f"{config.URL}/api/v1/trending?type=Movies&region={regioncode}"
    if popular == "most_popular_Games":
        apiurl = f"{config.URL}/api/v1/trending?type=Gaming&region={regioncode}"
    if popular == "most_popular_Music":
        apiurl = f"{config.URL}/api/v1/trending?type=Music&region={regioncode}"    

    # fetch api from invidious
    import subprocess, json

    # Reverted to a playlist-based approach for Featured/Most Viewed/Top
    # Rated: a curated YouTube playlist, not a live q=* search. This is
    # simpler and its entries are far less likely to have the
    # collab-credit authorId problem (e.g. "Warner Bros. and IMAX") that
    # search results ran into, since playlist entries are almost always
    # single real channels.
    #
    # Trade-off: the Today/This Week/All Time time-range tabs on Most
    # Viewed/Top Rated no longer actually filter anything — a fixed
    # curated playlist isn't scoped to a time window, so all three tabs
    # will show the same content now. If that filtering is wanted back,
    # it needs the search-based approach this replaces.
    if popular == "most_viewed":
        data = get_most_viewed_from_playlist()
    else:
        data = get_featured_from_hourly_playlists()

    print("POPULAR:", popular)
    
    import time, html

    now = time.time()
    cache_key = f"{popular}_{regioncode}"

    print("FEATURED RELOAD (no whole-feed cache):", cache_key)
    print("FEATURED COUNT:", len(data))

    # Will be used for checking Classic
    user_agent = request.headers.get('User-Agent').lower()
    
    # Templates have the / at the end, so let's remove it.
    if url[-1] == '/':
        url = url[:-1]

    if data:

        prompt = build_login_prompt(request)
        if prompt:
            data = [prompt] + data

        if config.SPYING == True:
            print_with_seperator("Region code: " + regioncode)

        display_data = data[:]
        random.shuffle(display_data)

        if "youtube/1.0.0" in user_agent or "youtube v1.0.0" in user_agent:
            return get.template('classic/featured.jinja2',{
                'data': display_data[:15],
                'unix': get.unix,
                'url': url
            })

        return get.template('featured.jinja2',{
            'data': display_data[:15],
            'unix': get.unix,
            'url': url
        })

    print("FRONTPAGE: data came back empty for", cache_key, flush=True)
    return get.error()

def get_most_viewed_from_playlist():
    playlist_id = "PLD8WkaWYGIio"
    playlist_url = f"https://www.youtube.com/playlist?list={playlist_id}"

    data = get_playlist_from_invidious(playlist_id)

    if not data:
        print("MOST VIEWED INVIDIOUS FAILED, USING YT-DLP")

        result = subprocess.run(
            [
                "yt-dlp",
                "--dump-json",
                "--ignore-errors",
                "--no-warnings",
                "--playlist-end", "30",
                playlist_url
            ],
            capture_output=True,
            text=True
        )

        data = []
        parsed_vids = []

        for line in result.stdout.splitlines():
            try:
                parsed_vids.append(json.loads(line))
            except Exception as e:
                print("MOST VIEWED PARSE ERROR:", e)

        # same fix as get_playlist_from_invidious() — resolve every
        # unique channel name concurrently first so normalize_video()'s
        # serial loop below hits a warm cache instead of making a live
        # search call per video, one at a time.
        unique_names = {
            (vid.get("uploader") or vid.get("channel") or vid.get("author"))
            for vid in parsed_vids
            if not (vid.get("uploader_id") or vid.get("channel_id") or vid.get("authorId"))
            and (vid.get("uploader") or vid.get("channel") or vid.get("author"))
        }
        uncached_names = [n for n in unique_names if n not in channel_id_cache]
        if uncached_names:
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = [executor.submit(get_channel_id_from_name, n) for n in uncached_names]
                for future in as_completed(futures):
                    future.result()

        for vid in parsed_vids:
            item = normalize_video(vid)

            if item:
                data.append(item)

    random.shuffle(data)
    data = data[:15]
    data = enrich_view_counts(data, force_refresh=True)

    return data

@video.route("/feeds/api/most_viewed")
@video.route("/<int:res>/feeds/api/most_viewed")
def most_viewed(res=''):

    print("MOST VIEWED ROUTE HIT")

    # Clamp res
    if type(res) == int:
        res = min(max(res, 144), config.RESMAX)

    url = request.url_root + str(res)

    print("MOST VIEWED RELOAD (no whole-feed cache)")

    # Reverted to the playlist-based approach — see the comment in
    # frontpage() for why. Note this means the Today/This Week/All Time
    # tabs no longer filter anything here either.
    data = get_most_viewed_from_playlist()

    if url[-1] == '/':
        url = url[:-1]

    print("MOST VIEWED COUNT:", len(data))

    prompt = build_login_prompt(request)
    if prompt:
        data = [prompt] + data

    user_agent = request.headers.get('User-Agent', '').lower()

    offset = int(time.time()) % len(data) if data else 0
    data = data[offset:] + data[:offset]

    if "youtube/1.0.0" in user_agent or "youtube v1.0.0" in user_agent:
        return get.template('classic/featured.jinja2', {
            'data': data[:15],
            'unix': get.unix,
            'url': url
        })

    return get.template('featured.jinja2', {
        'data': data[:15],
        'unix': get.unix,
        'url': url
    })

def cleanup_old_files():
    folder = "static"
    max_age = 600  # seconds (10 minutes)

    while True:
        now = time.time()

        for filename in os.listdir(folder):
            if filename.endswith(".mp4"):
                path = os.path.join(folder, filename)

                if os.path.isfile(path):
                    age = now - os.path.getmtime(path)

                    if age > max_age:
                        try:
                            os.remove(path)
                            print("Deleted:", filename)
                        except Exception as e:
                            print("Delete failed:", e)

        time.sleep(60)  # check every 1 minute


# search for videos
def _handle_batch_request(res=''):
    """Shared GData batch-query handler. Multiple feeds (History's
    /feeds/api/videos/batch, Featured's declared .../recently_featured/batch
    link, etc.) all POST the same kind of <feed><entry><id>...</id></entry>
    batch body and expect the same kind of response — so this is reused
    across routes instead of duplicated."""
    if type(res) == int:
        res = min(max(res, 144), config.RESMAX)

    url = request.url_root + str(res)
    if url[-1] == '/':
        url = url[:-1]

    try:
        body = request.get_data(as_text=True)
        root = ET.fromstring(body)
    except Exception as e:
        print("BATCH PARSE ERROR:", e, flush=True)
        return get.error()

    ns = {"atom": "http://www.w3.org/2005/Atom"}

    video_ids = []
    for entry in root.findall("atom:entry", ns):
        id_el = entry.find("atom:id", ns)
        if id_el is not None and id_el.text:
            video_ids.append(id_el.text.strip().rsplit("/", 1)[-1])

    print("BATCH VIDEO IDS:", video_ids, flush=True)

    clean = []
    for vid_id in video_ids:
        data = get.fetch(f"{config.URL}/api/v1/videos/{vid_id}")
        if not data:
            continue
        item = normalize_video(data)
        if item:
            clean.append(item)

    print("BATCH RESOLVED COUNT:", len(clean), flush=True)

    if not clean:
        clean = [build_empty_history_notice()]

    return get.template("batch_videos.jinja2", {
        "data": clean,
        "unix": get.unix,
        "url": url,
    })

@video.route("/feeds/api/videos/batch", methods=["GET"])
@video.route("/<int:res>/feeds/api/videos/batch", methods=["GET"])
def videos_batch_get(res=''):
    """History (GET variant) — older app versions (iOS 3) appear to GET
    this endpoint directly instead of POSTing a body, previously a hard
    404 since only POST was registered. Dedicated function (not shared
    with standardfeeds' batch handler) specifically so we can see
    whether iOS 3 is even sending a recognizable device/token — if it
    isn't, that's likely why History never resolves anything, since the
    device the videos would belong to can't be identified at all."""
    if type(res) == int:
        res = min(max(res, 144), config.RESMAX)
    url = request.url_root + str(res)
    if url[-1] == '/':
        url = url[:-1]

    device_id = extract_device_id(request)
    access_token = get_valid_access_token(device_id)
    print("VIDEOS_BATCH_GET device_id:", device_id, "| has_access_token:", bool(access_token), flush=True)

    print("VIDEOS_BATCH_GET query string:", request.query_string.decode('utf-8', 'ignore'), flush=True)
    print("VIDEOS_BATCH_GET args:", dict(request.args), flush=True)

    video_ids = list(request.args.getlist("id"))
    for key in ("ids", "video_id", "videoIds", "videoid"):
        val = request.args.get(key)
        if val:
            video_ids += [v.strip() for v in val.split(",") if v.strip()]

    seen = set()
    video_ids = [v for v in video_ids if not (v in seen or seen.add(v))]
    print("VIDEOS_BATCH_GET resolved IDs:", video_ids, flush=True)

    clean = []
    for vid_id in video_ids:
        data = get.fetch(f"{config.URL}/api/v1/videos/{vid_id}")
        if not data:
            continue
        item = normalize_video(data)
        if item:
            clean.append(item)

    print("VIDEOS_BATCH_GET resolved count:", len(clean), flush=True)

    # The client apparently never actually sends video IDs to resolve on
    # this endpoint (confirmed: fully empty body/query args even when
    # authenticated) — History needs to come from the account's real
    # watch history instead, fetched directly via the same internal
    # YouTube feed mechanism already used for Subscriptions
    # (FEsubscriptions), just with the FEhistory browseId instead.
    if not clean and access_token:
        try:
            history_data = fetch_personal_feed(access_token, "FEhistory", limit=25)
            print("VIDEOS_BATCH_GET FEhistory fetched:", len(history_data), flush=True)
            clean = history_data
        except Exception as e:
            print("VIDEOS_BATCH_GET FEhistory ERROR:", repr(e), flush=True)

    if not clean:
        clean = [build_empty_history_notice()]

    return get.template("batch_videos.jinja2", {
        "data": clean,
        "unix": get.unix,
        "url": url,
    })

@video.route("/feeds/api/videos/batch", methods=["POST"])
@video.route("/<int:res>/feeds/api/videos/batch", methods=["POST"])
def videos_batch(res=''):
    """History (POST variant) — dedicated function, not shared with
    standardfeeds' generic batch handler. The app keeps its own local
    list of watched video IDs on-device and POSTs them here as a batch
    <feed> of <entry><id>...</id></entry> elements, expecting metadata
    for each back in one response.

    Logs device_id/access_token explicitly (this previously had no
    token-checking at all, sharing _handle_batch_request with routes
    that genuinely don't need it) — if iOS 3 isn't sending a
    recognizable device/token here, that would explain History coming
    back empty regardless of what video IDs it posts, since we can't
    tell which linked account it belongs to."""
    if type(res) == int:
        res = min(max(res, 144), config.RESMAX)

    url = request.url_root + str(res)
    if url[-1] == '/':
        url = url[:-1]

    device_id = extract_device_id(request)
    access_token = get_valid_access_token(device_id)
    print("VIDEOS_BATCH(POST) device_id:", device_id, "| has_access_token:", bool(access_token), flush=True)

    try:
        body = request.get_data(as_text=True)
        print("VIDEOS_BATCH(POST) raw body:", repr(body[:500]), flush=True)
        root = ET.fromstring(body)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        video_ids = []
        for entry in root.findall("atom:entry", ns):
            id_el = entry.find("atom:id", ns)
            if id_el is not None and id_el.text:
                video_ids.append(id_el.text.strip().rsplit("/", 1)[-1])
    except Exception as e:
        print("VIDEOS_BATCH(POST) PARSE ERROR (likely an empty/non-XML body):", e, flush=True)
        video_ids = []

    print("VIDEOS_BATCH(POST) video IDs:", video_ids, flush=True)

    clean = []
    for vid_id in video_ids:
        data = get.fetch(f"{config.URL}/api/v1/videos/{vid_id}")
        if not data:
            continue
        item = normalize_video(data)
        if item:
            clean.append(item)

    print("VIDEOS_BATCH(POST) resolved count:", len(clean), flush=True)

    # Same as the GET variant: the client evidently doesn't reliably
    # send video IDs to resolve here — fall back to the account's real
    # watch history via YouTube's internal FEhistory feed.
    if not clean and access_token:
        try:
            history_data = fetch_personal_feed(access_token, "FEhistory", limit=25)
            print("VIDEOS_BATCH(POST) FEhistory fetched:", len(history_data), flush=True)
            clean = history_data
        except Exception as e:
            print("VIDEOS_BATCH(POST) FEhistory ERROR:", repr(e), flush=True)

    if not clean:
        clean = [build_empty_history_notice()]

    return get.template("batch_videos.jinja2", {
        "data": clean,
        "unix": get.unix,
        "url": url,
    })

@video.route("/feeds/api/standardfeeds/<regioncode>/<popular>/batch", methods=["GET"])
@video.route("/feeds/api/standardfeeds/<popular>/batch", methods=["GET"])
@video.route("/<int:res>/feeds/api/standardfeeds/<regioncode>/<popular>/batch", methods=["GET"])
@video.route("/<int:res>/feeds/api/standardfeeds/<popular>/batch", methods=["GET"])
def standardfeeds_batch_get(regioncode="US", popular=None, res=''):
    """This same link is hardcoded into uploads.jinja2 (used by
    Favorites, Channel Uploads, My Videos) as well as Featured — the app
    appears to GET it a few seconds after those load, which previously
    405'd since only POST was registered. Same query-param guessing
    approach as videos_batch_get, since we don't have a real capture of
    the exact shape this GET uses."""
    if type(res) == int:
        res = min(max(res, 144), config.RESMAX)
    url = request.url_root + str(res)
    if url[-1] == '/':
        url = url[:-1]

    print("STANDARDFEEDS_BATCH_GET query string:", request.query_string.decode('utf-8', 'ignore'), flush=True)
    print("STANDARDFEEDS_BATCH_GET args:", dict(request.args), flush=True)

    video_ids = list(request.args.getlist("id"))
    for key in ("ids", "video_id", "videoIds", "videoid"):
        val = request.args.get(key)
        if val:
            video_ids += [v.strip() for v in val.split(",") if v.strip()]

    seen = set()
    video_ids = [v for v in video_ids if not (v in seen or seen.add(v))]
    print("STANDARDFEEDS_BATCH_GET resolved IDs:", video_ids, flush=True)

    clean = []
    for vid_id in video_ids:
        data = get.fetch(f"{config.URL}/api/v1/videos/{vid_id}")
        if not data:
            continue
        item = normalize_video(data)
        if item:
            clean.append(item)

    return get.template("batch_videos.jinja2", {
        "data": clean,
        "unix": get.unix,
        "url": url,
    })

@video.route("/feeds/api/standardfeeds/<regioncode>/<popular>/batch", methods=["POST"])
@video.route("/feeds/api/standardfeeds/<popular>/batch", methods=["POST"])
@video.route("/<int:res>/feeds/api/standardfeeds/<regioncode>/<popular>/batch", methods=["POST"])
@video.route("/<int:res>/feeds/api/standardfeeds/<popular>/batch", methods=["POST"])
def standardfeeds_batch(regioncode="US", popular=None, res=''):
    """The Featured/Most Viewed template declares this batch link on
    every response (<link rel='...#batch' href='.../batch' />). It looks
    like some app action (e.g. Favorites' 'merge' prompt) POSTs to
    whatever batch link it currently has cached — even from an unrelated
    screen — and previously got a hard 404 here, which is the likely
    trigger for the forced sign-out. Handling it the same way as the
    History batch endpoint avoids that."""
    return _handle_batch_request(res)

@video.route("/feeds/api/videos/<video_id>")
@video.route("/<int:res>/feeds/api/videos/<video_id>")
def single_video_info(video_id, res=''):
    """Plain GET for one video's own info — no /related or /comments
    suffix. The app hits this directly in some situations (relaunching
    mid-video, reloading the Info tab) rather than going through the
    /batch POST endpoint. Previously 404'd since only the suffixed
    routes and the POST batch endpoint existed."""
    if type(res) == int:
        res = min(max(res, 144), config.RESMAX)

    url = request.url_root + str(res)
    if url[-1] == '/':
        url = url[:-1]

    data = get.fetch(f"{config.URL}/api/v1/videos/{video_id}")
    if not data:
        return get.error()

    item = normalize_video(data)
    if not item:
        return get.error()

    return get.template("batch_videos.jinja2", {
        "data": [item],
        "unix": get.unix,
        "url": url,
    })

@video.route("/feeds/api/videos")
@video.route("/feeds/api/videos/")
@video.route("/<int:res>/feeds/api/videos")
@video.route("/<int:res>/feeds/api/videos/")
def search_videos(res=''):

    # Clamp Res
    if type(res) == int:
        res = min(max(res, 144), config.RESMAX)
    
    url = request.url_root + str(res)
    currentPage, next_page = helpers.process_start_index(request)

    user_agent = request.headers.get('User-Agent').lower()

    search_keyword = request.args.get('q')

    if not search_keyword:
        # "Most Recent" tab: the classic app calls this same endpoint with
        # no q=, just orderby=updated, expecting a general recent-videos
        # feed rather than a search. Reverted back to this search-based
        # approach per feedback — trending had other tradeoffs that were
        # worse than this one's stale dates.
        #
        # sort_by=upload_date alone isn't reliably honored against a
        # wildcard q=* on this instance — it still surfaces videos from
        # 2020/2021 etc. Layering Invidious's own date= recency filter on
        # top forces genuinely-recent (current-year) results.
        #
        # A narrow window (e.g. "week") can come back with only a couple
        # of items on this instance — accepting that outright leaves the
        # tab looking half-empty. On top of that, a single "q=*" wildcard
        # search is itself too sparse on this instance (date=month has
        # come back completely empty every time, and week/year each only
        # turn up a couple of items) — so cross several different broad,
        # generic terms with each date window and merge everything
        # together (deduped by videoId), rather than relying on one
        # query to carry the whole feed.
        #
        # Deliberately NOT falling back to an unfiltered search: that
        # reliably surfaced 2020-2022 videos on this instance, worse than
        # a shorter but genuinely recent list. RECENCY_CUTOFF_DAYS below
        # is a hard safety net on top of this either way, in case a
        # "recent" window's date filter isn't actually enforced
        # server-side for some queries.
        MIN_DESIRED = 15
        RECENCY_CUTOFF_DAYS = 90
        GENERIC_QUERIES = ("*", "official", "new", "2026", "live", "vlog", "music", "gameplay")
        data = []
        seen_ids = set()
        for date_filter in ("week", "month", "year"):
            date_qs = f"&date={date_filter}"

            def _fetch_query(q):
                return q, get.fetch(
                    f"{config.URL}/api/v1/search"
                    f"?q={q}&type=video&sort_by=upload_date&region=US{date_qs}"
                ) or []

            with ThreadPoolExecutor(max_workers=len(GENERIC_QUERIES)) as executor:
                results = list(executor.map(_fetch_query, GENERIC_QUERIES))

            for q, batch in results:
                new_count = 0
                for item in batch:
                    vid_id = item.get("videoId") or item.get("id")
                    if vid_id and vid_id not in seen_ids:
                        seen_ids.add(vid_id)
                        data.append(item)
                        new_count += 1
                print(
                    "MOST RECENT: date filter", date_filter, "query", repr(q),
                    "->", len(batch), "raw items,", new_count, "new,",
                    len(data), "total so far", flush=True
                )
            if len(data) >= MIN_DESIRED:
                break
        if not data:
            return get.error()

        # Hard safety net: drop anything older than RECENCY_CUTOFF_DAYS
        # regardless of which date-filter tier it came from, since the
        # date filter itself isn't fully trustworthy on this instance.
        cutoff_ts = time.time() - (RECENCY_CUTOFF_DAYS * 86400)
        before_cutoff_count = len(data)
        data = [
            item for item in data
            if safe_published(item.get("published") or item.get("publishedText")) >= cutoff_ts
        ]
        print(
            "MOST RECENT: recency cutoff dropped",
            before_cutoff_count - len(data), "of", before_cutoff_count,
            "items older than", RECENCY_CUTOFF_DAYS, "days", flush=True
        )
        if not data:
            return get.error()

        clean = []
        for vid in data:
            # classic YouTube can't play live streams — skip them
            if vid.get("type") == "livestream" or vid.get("liveNow") or vid.get("isLive") or vid.get("isUpcoming"):
                continue
            item = normalize_video(vid)
            if item:
                clean.append(item)

        print("MOST RECENT: after live-filter + normalize:", len(clean), "items", flush=True)

        # backfills any remaining gaps in stats/author, same as before
        clean = enrich_view_counts(clean)
        print("MOST RECENT: after enrichment:", len(clean), "items", flush=True)
        clean = [item for item in clean if not item.get("_is_live")]
        print("MOST RECENT: after _is_live filter:", len(clean), "items", flush=True)

        if url[-1] == '/':
            url = url[:-1]

        if "youtube/1.0.0" in user_agent or "youtube v1.0.0" in user_agent:
            return get.template('classic/featured.jinja2', {
                'data': clean[:15],
                'unix': get.unix,
                'url': url
            })

        return get.template('featured.jinja2', {
            'data': clean[:15],
            'unix': get.unix,
            'url': url
        })
    
    # print logs if enabled
    if config.SPYING == True:
        print_with_seperator('Searched: ' + search_keyword)

    # remove space character
    raw_search_keyword = search_keyword
    search_keyword = search_keyword.replace(" ", "%20")

    max_results = int(request.args.get("max-results", 10))
    start_index = int(request.args.get("start-index", 1))

    invidious_page = ((start_index - 1) // max_results) + 1
    next_start = start_index + max_results

    next_page = f"{url}/feeds/api/videos?q={raw_search_keyword}&start-index={next_start}&max-results={max_results}"

    # q and page is already made, so lets hand add it
    query = f'q={search_keyword}&type=video&page={currentPage}'
    
    # If we have orderby, turn it into invidious friendly parameters
    # Else ignore it
    orderby = request.args.get('orderby')
    if orderby in helpers.valid_search_orderby:
        query += f'&sort={helpers.valid_search_orderby[orderby]}'

    # If we have time, turn it into invidious friendly parameters
    # Else ignore it
    time_param = request.args.get('time')
    if time_param in helpers.valid_search_time:
        query += f'&date={helpers.valid_search_time[time_param]}'

    # If we have duration, turn it into invidious friendly parameters
    # Else ignore it
    duration = request.args.get('duration')
    if duration in helpers.valid_search_duration:
        query += f'&duration={helpers.valid_search_duration[duration]}'
    
    # If we have captions, turn it into invidious friendly parameters
    # Else ignore it
    # NOTE: YouTube 1.1.0 app only supports subtitles in the search
    caption = request.args.get('caption')
    if type(caption) == str and caption.lower() == 'true':
        query += '&features=subtitles'

    # Santize and stitch 
    query = query.replace('&', '&amp;')

    # search by videos
    # search by videos using Invidious
    import requests

    try:
        r = requests.get(
            f"{config.URL}/api/v1/search",
            params={
                "q": raw_search_keyword,
                "type": "video",
                "page": invidious_page
            },
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json"
            },
            timeout=10
        )

        r.raise_for_status()
        json_data = r.json()
        print("REQUEST ARGS:", dict(request.args))
        print("INVIDIOUS PAGE:", invidious_page)
        print("RAW INVIDIOUS COUNT:", len(json_data))

        data = []

        for index, vid in enumerate(json_data):
            print("SEARCH INDEX:", index, "TYPE:", vid.get("type"), "TITLE:", vid.get("title"))

            vid_id = vid.get("videoId") or vid.get("id")

            if not vid_id:
                continue

            title = vid.get("title") or ""
            author = vid.get("author") or "Unknown"
            duration = vid.get("lengthSeconds") or vid.get("duration") or 0
            view_count = vid.get("viewCount") or vid.get("view_count") or 0
            availability = vid.get("availability")

            if availability in ["private", "premium_only", "subscriber_only", "needs_auth", "unlisted"]:
                continue

            if not title.strip():
                continue

            if not duration:
                duration = 0

            if vid.get("type") == "livestream" or vid.get("liveNow") or vid.get("isLive"):
                continue

            
            print("ADDING:", title, vid_id, duration)

            published = vid.get("published") or vid.get("timestamp") or 0

            if not published and vid.get("publishedText"):
                published = 0

            item = {
                "title": html.escape(str(title)),
                "videoId": vid_id,
                "author": html.escape(space_safe_channel_name(str(author))),
                "authorId": vid.get("authorId") or vid.get("uploader_id") or vid.get("channel_id") or resolve_channel_id_for_credit(vid.get("author") or vid.get("uploader") or vid.get("channel")) or  "unknown",
                "viewCount": int(view_count or 0),
                "lengthSeconds": int(duration or 0),
                "published": int(published or 0),
                "publishedText": vid.get("publishedText") or "",
                "description": html.escape(str(vid.get("description") or ""))
            }

            print(
                "AUTHOR:", item["author"],
                "AUTHORID:", item["authorId"],
                flush=True
            )

            print("UPLOADER ID:", item["author"], item["authorId"], flush=True)

            print(
                "TITLE:", item["title"],
                "DESC:", repr(item["description"][:100]),
                flush=True
            )

            item["authorId"] = (
                item.get("authorId")
                if item.get("authorId") not in ["unknown", "Unknown", "", None]
                else get_channel_id_from_name(item.get("author"))
            )

            print(
                "UPLOADER DEBUG:",
                item.get("author"),
                item.get("authorId"),
                flush=True
            )

            #item = apply_cached_metadata(item)
            data.append(item)

    except Exception as e:
        print("INVIDIOUS SEARCH ERROR:", e)
        data = []

    data = enrich_view_counts(data)

    # Templates have the / at the end, so let's remove it.
    if url[-1] == '/':
        url = url[:-1]

    if not data:
        next_page = None

    print("FINAL SEARCH COUNT:", len(data))
    
    # classic tube check
    if "youtube/1.0.0" in user_agent or "youtube v1.0.0" in user_agent:
        print("FINAL DATA COUNT:", len(data))
        print("IDS:", [x["videoId"] for x in data])
        return get.template('classic/search.jinja2',{
            'data': data,
            'unix': get.unix,
            'url': url,
            'next_page': next_page
        })
    else:
        return get.template('search_results.jinja2',{
            'data': data,
            'unix': get.unix,
            'url': url,
            'next_page': next_page
        })

# video's comments
# IDEA: filter the comments too?
@video.route("/api/videos/<videoid>/comments", methods=["POST"])
@video.route("/<int:res>/api/videos/<videoid>/comments", methods=["POST"])
@video.route("/feeds/api/videos/<videoid>/comments", methods=["POST"])
@video.route("/<int:res>/feeds/api/videos/<videoid>/comments", methods=["POST"])
def post_comment(videoid, res=''):
    """Post a comment, backed by YouTube Data API v3's
    commentThreads.insert. The app POSTs a body like:
        <entry ...><content>the comment text</content></entry>"""
    device_id = extract_device_id(request)
    access_token = get_valid_access_token(device_id)
    if not access_token:
        return get.error()

    try:
        body = request.get_data(as_text=True)
        root = ET.fromstring(body)
    except Exception as e:
        print("POST_COMMENT PARSE ERROR:", e, flush=True)
        return get.error()

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    content_el = root.find("atom:content", ns)
    if content_el is None:
        content_el = root.find("content")

    comment_text = content_el.text.strip() if content_el is not None and content_el.text else None
    if not comment_text:
        print("POST_COMMENT: no comment text found in body", flush=True)
        return get.error()

    try:
        r = requests.post(
            "https://www.googleapis.com/youtube/v3/commentThreads",
            params={"part": "snippet"},
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={
                "snippet": {
                    "videoId": videoid,
                    "topLevelComment": {
                        "snippet": {"textOriginal": comment_text}
                    }
                }
            },
            timeout=10,
        )
        print("POST_COMMENT status:", r.status_code, flush=True)
        if not r.ok:
            print("POST_COMMENT body:", r.text[:400], flush=True)
            return get.error()
    except Exception as e:
        print("POST_COMMENT ERROR:", repr(e), flush=True)
        return get.error()

    return Response(
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<entry xmlns='http://www.w3.org/2005/Atom' xmlns:yt='http://gdata.youtube.com/schemas/2007'>"
        f"<content>{html.escape(comment_text)}</content>"
        "</entry>",
        mimetype="application/atom+xml"
    )

@video.route("/api/videos/<videoid>/comments")
@video.route("/<int:res>/api/videos/<videoid>/comments")
@video.route("/feeds/api/videos/<videoid>/comments")
@video.route("/<int:res>/feeds/api/videos/<videoid>/comments")
def comments(videoid, res=''):
    
    # Clamp Res
    if type(res) == int:
        res = min(max(res, 144), config.RESMAX)
    
    url = request.url_root + str(res) 

    continuation_token = request.args.get('continuation') and '&amp;continuation=' + request.args.get('continuation') or ''
    # fetch invidious comments api
    data = get.fetch(f"{config.URL}/api/v1/comments/{videoid}?sortby={config.SORT_COMMENTS}{continuation_token}")

    # Templates have the / at the end, so let's remove it.
    if url[-1] == '/':
        url = url[:-1]
    if data:
        # NOTE: No comments sometimes returns {'error': 'Comments not found.'}
        if 'error' in data:
            comments = None
        else:
            comments = data['comments']
        return get.template('comments.jinja2',{
            'data': comments,
            'unix': get.unix,
            'url': url,
            'continuation': 'continuation' in data and data['continuation'] or None,
            'video_id': videoid
        })

    return get.error()

@video.route("/thumb/<video_id>")
def thumbnail(video_id):
    import requests
    from flask import Response

    thumb_url = f"https://i.ytimg.com/vi/{video_id}/default.jpg"

    try:
        r = requests.get(thumb_url, timeout=10)

        response = Response(r.content, mimetype="image/jpeg")
        response.headers["Content-Type"] = "image/jpeg"
        response.headers["Cache-Control"] = "public, max-age=86400"
        response.headers["Content-Length"] = str(len(r.content))
        return response

    except Exception as e:
        print("THUMB ERROR:", e)
        return "", 404

@video.route("/getvideo/<video_id>")
@video.route("/<int:res>/getvideo/<video_id>")
def getvideo(video_id, res=None):
    if video_id == "login_prompt":
        return "This isn't a real video — check its description for the login link.", 200

    import subprocess
    import json
    import os
    import time

    t0 = time.time()
    lock = _get_download_lock(video_id)
    lock.acquire()
    _download_semaphore.acquire()

    try:
        print("START GETVIDEO:", video_id)
        
        t = time.time()
        url = f"https://www.youtube.com/watch?v={video_id}"
        print("URL BUILD SECONDS:", time.time() - t)

        t = time.time()
        temp_output = f"static/{video_id}.mp4"
        print("PATH BUILD SECONDS:", time.time() - t)

        t = time.time()

        exists = os.path.exists(temp_output)

        print("CACHE CHECK SECONDS:", time.time() - t)

        if exists:
            print("CACHE HIT:", video_id)
            return redirect(f"/static/{video_id}.mp4", 302)

        print("ABOUT TO START YT-DLP:", video_id)

        # -------- STEP 1: TRY INSTANT STREAM --------
        '''result = subprocess.run(
            ["yt-dlp", "-j", "-f", "36/18/17", "--no-playlist", url],
            capture_output=True,
            text=True
        )

        if result.returncode != 0 or not result.stdout:
            print("yt-dlp failed:", result.stderr)
            raise Exception("yt-dlp failed")

        data = json.loads(result.stdout)

        format_id = data.get("format_id", "")
        ext = data.get("ext", "")

        # Only allow safe formats for iPhone 3G
        if ext == "3gp":
            stream_url = data.get("url")
            if stream_url:
                print("Using SAFE DIRECT STREAM:", format_id)
                return redirect(stream_url, 302)

        print("Stream not compatible, converting...")

        # -------- STEP 2: FALLBACK TO CONVERSION --------
        print("Falling back to conversion...")'''

        temp_input = f"temp_{video_id}.mp4"
        temp_output = f"static/{video_id}.mp4"

        best_encoder = _detect_best_video_encoder()

        piped_success = False
        t2 = time.time()

        # -------- Try piping yt-dlp directly into ffmpeg --------
        # The previous approach downloaded the source to disk, then had
        # ffmpeg read it back — a full write+read of the same data for
        # nothing. Piping yt-dlp's stdout straight into ffmpeg's stdin
        # removes that entire round trip. This does NOT attempt true
        # progressive playback during encoding (the output container is
        # still a full +faststart mp4, unchanged) — just gets to that
        # same finished file faster. Falls back completely to the
        # original disk-based method if piping fails for any reason
        # (some videos/formats don't pipe cleanly).
        try:
            print("TRYING PIPED YT-DLP -> FFMPEG", flush=True)

            ytdlp_proc = subprocess.Popen([
                "yt-dlp",
                "--extractor-args", "youtube:player_client=android",
                "-f", "18/36/17",
                "--no-playlist",
                "--no-warnings",
                "-o", "-",
                url
            ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

            ffmpeg_proc = subprocess.Popen([
                "ffmpeg", "-y",
                "-i", "pipe:0",
                "-vf", "scale=320:240",
                *best_encoder,
                "-level", "3.0",
                "-pix_fmt", "yuv420p",

                "-c:a", "aac",
                "-profile:a", "aac_low",
                "-b:a", "96k",
                "-ar", "44100",
                "-ac", "2",

                "-fflags", "+genpts",
                "-movflags", "+faststart",

                temp_output
            ], stdin=ytdlp_proc.stdout, stderr=subprocess.PIPE)

            ytdlp_proc.stdout.close()  # let ffmpeg own the read end
            _, ffmpeg_err = ffmpeg_proc.communicate(timeout=120)
            ytdlp_proc.wait(timeout=5)

            if (
                ffmpeg_proc.returncode == 0
                and os.path.exists(temp_output)
                and os.path.getsize(temp_output) > 0
            ):
                piped_success = True
                print("PIPED PIPELINE SUCCEEDED in", time.time() - t2, "seconds", flush=True)
            else:
                print("PIPED PIPELINE FAILED:", ffmpeg_err[-400:] if ffmpeg_err else "no stderr", flush=True)
                if os.path.exists(temp_output):
                    os.remove(temp_output)

        except Exception as e:
            print("PIPED PIPELINE ERROR:", repr(e), flush=True)
            if os.path.exists(temp_output):
                os.remove(temp_output)

        if not piped_success:
            print("FALLING BACK TO DISK-BASED DOWNLOAD+CONVERT", flush=True)

            # Download
            try:
                print("TRYING NORMAL YT-DLP")

                subprocess.run([
                    "yt-dlp",
                    "--extractor-args", "youtube:player_client=android",
                    "-f", "18/36/17",
                    "--no-playlist",
                    "--no-warnings",
                    "-o", temp_input,
                    url
                ],
                check=True)

            except subprocess.CalledProcessError as e:

                print("NORMAL FAILED, TRYING ANDROID")

                subprocess.run([
                    "yt-dlp",
                    "--extractor-args", "youtube:player_client=android",
                    "-f", "18/36/17",
                    "--no-playlist",
                    "--no-warnings",
                    "-o", temp_input,
                    url
                ],
                check=True)

            print("START FFMPEG")
            t3 = time.time()

            # Try hardware-accelerated encoding first — software libx264
            # is CPU-bound no matter how fast the preset is.
            hw_encoders = [
                ["-c:v", "h264_nvenc", "-preset", "p1", "-profile:v", "baseline"],
                ["-c:v", "h264_qsv", "-preset", "veryfast", "-profile:v", "baseline"],
            ]

            encoded = False
            for hw_args in hw_encoders:
                try:
                    subprocess.run([
                        "ffmpeg",
                        "-i", temp_input,
                        "-vf", "scale=320:240",
                        *hw_args,
                        "-level", "3.0",
                        "-pix_fmt", "yuv420p",

                        "-c:a", "aac",
                        "-profile:a", "aac_low",
                        "-b:a", "96k",
                        "-ar", "44100",
                        "-ac", "2",

                        "-fflags", "+genpts",
                        "-movflags", "+faststart",

                        temp_output
                    ], check=True, capture_output=True)
                    print("HARDWARE ENCODE SUCCEEDED:", hw_args[1], flush=True)
                    encoded = True
                    break
                except Exception as e:
                    print("HARDWARE ENCODE FAILED, trying next option:", hw_args[1], flush=True)
                    if os.path.exists(temp_output):
                        os.remove(temp_output)

            if not encoded:
                # known-working software fallback — unchanged from before
                subprocess.run([
                    "ffmpeg",
                    "-i", temp_input,
                "-vf", "scale=320:240",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-profile:v", "baseline",
                "-level", "3.0",
                "-pix_fmt", "yuv420p",

                "-c:a", "aac",
                "-profile:a", "aac_low",
                "-b:a", "96k",
                "-ar", "44100",
                "-ac", "2",

                "-fflags", "+genpts",
                "-movflags", "+faststart",

                temp_output
            ], check=True)

            print("FALLBACK FFMPEG SECONDS:", time.time() - t3)

        print("TOTAL GETVIDEO SECONDS:", time.time() - t0)

        if os.path.exists(temp_input):
            os.remove(temp_input)

        return redirect(f"/static/{video_id}.mp4", 302)

    except Exception as e:
        print("HYBRID ERROR:", e)
        return "Playback failed", 500

    finally:
        lock.release()
        _download_semaphore.release()

@video.route("/feeds/api/videos/<video_id>/related")
@video.route("/<int:res>/feeds/api/videos/<video_id>/related")
def get_suggested(video_id, res=''):
    data = []

    # 1. Try real Invidious/Companion related videos
    try:
        data = get_relevant_videos(video_id) or []
        print("REAL RELATED COUNT:", len(data))
    except Exception as e:
        print("REAL RELATED FAILED:", e)
        data = []

    # 2. Fast fake-related fallback using Invidious search
    if not data:
        print("FALLING BACK TO FAST INVIDIOUS SEARCH")

        video_info = get.fetch(
            f"{config.URL}/api/v1/search?q={video_id}&type=video"
        ) or []

        search_query = "trending videos"

        if isinstance(video_info, list) and video_info:
            first = video_info[0]
            title = first.get("title") or ""
            author = first.get("author") or ""

            # Prefer title first, author second
            if title:
                search_query = title
            elif author and author.lower() not in ["unknown", "none"]:
                search_query = f"{author} videos"

        print("RELATED SEARCH QUERY:", search_query)

        raw = get.fetch(
            f"{config.URL}/api/v1/search?q={search_query}&type=video"
        ) or []

        for vid in raw:
            vid_id = vid.get("videoId") or vid.get("id")
            vid_title = vid.get("title")

            if not vid_id or not vid_title:
                continue

            if vid_id == video_id:
                continue

            data.append({
                "title": html.escape(str(vid_title)),
                "videoId": vid_id,
                "author": html.escape(space_safe_channel_name(str(vid.get("author") or "Unknown"))),
                "authorId": vid.get("authorId") or resolve_channel_id_for_credit(vid.get("author") or vid.get("uploader") or vid.get("channel")) or "unknown",
                "viewCount": safe_int(vid.get("viewCount") or 0),
                "lengthSeconds": safe_int(vid.get("lengthSeconds") or 0),
                "published": safe_published(vid.get("published") or 0),
                "description": html.escape(str(vid.get("description") or "")[:1000])
            })

        data = data[:10]

    url = request.url_root + str(res)
    user_agent = request.headers.get('User-Agent', '').lower()

    if url[-1] == '/':
        url = url[:-1]

    print("RELATED FINAL COUNT:", len(data))
    print("RELATED DATA SAMPLE:", data[:2])

    if "youtube/1.0.0" in user_agent or "youtube v1.0.0" in user_agent:
        return get.template('classic/search.jinja2',{
            'data': data,
            'unix': get.unix,
            'url': url,
            'next_page': None
        })

    return get.template('search_results.jinja2',{
        'data': data,
        'unix': get.unix,
        'url': url,
        'next_page': None
    })