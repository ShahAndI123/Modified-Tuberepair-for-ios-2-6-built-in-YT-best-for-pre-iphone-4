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
import time
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

video = Blueprint("video", __name__)

featured_cache = {}
featured_cache_time = {}

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

def enrich_view_counts(items, max_workers=10):
    """Fetch real stats (views, duration, published date) for items that
    are missing them, concurrently instead of one-at-a-time — sequential
    per-item calls on a 20-30 item feed is what caused Top Rated to time
    out."""
    to_fetch = [
        item for item in items
        if item and (not item.get("viewCount") or not item.get("lengthSeconds"))
    ]
    if not to_fetch:
        return items

    def _fetch(item):
        try:
            stats = get.fetch(
                f"{config.URL}/api/v1/videos/{item['videoId']}"
                f"?fields=viewCount,lengthSeconds,published"
            )
            if stats:
                if not item.get("viewCount"):
                    item["viewCount"] = int(stats.get("viewCount") or 0)
                if not item.get("lengthSeconds"):
                    item["lengthSeconds"] = int(stats.get("lengthSeconds") or 0)
                if not item.get("published"):
                    item["published"] = int(stats.get("published") or 0)
        except Exception as e:
            print("STATS ENRICH FAILED:", item.get("videoId"), e, flush=True)
        return item

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_fetch, item) for item in to_fetch]
        for future in as_completed(futures):
            future.result()

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

    return {
        "title": html.escape(str(title)),
        "videoId": str(video_id),
        "author": html.escape(str(
            vid.get("author")
            or vid.get("uploader")
            or vid.get("channel")
            or "Unknown"
        )),
        "authorId": str(
            vid.get("authorId")
            or vid.get("uploader_id")
            or vid.get("channel_id")
            or get_channel_id_from_name(vid.get("author") or vid.get("uploader") or vid.get("channel"))
            or "unknown"
        ),
        "viewCount": safe_int(
            vid.get("viewCount")
            or vid.get("view_count")
            or vid.get("views")
        ),
        "lengthSeconds": int(vid.get("lengthSeconds") or vid.get("duration") or 0),
        "published": safe_published(
            vid.get("published") or vid.get("timestamp") or vid.get("publishedText")
        ),
        "description": html.escape(str(vid.get("description") or "")[:120])
    }

    print("RELATED VIDEO KEYS:", vid.keys())

@video.route("/feeds/api/users/<channel>")
@video.route("/feeds/api/users/<channel>/")
@video.route("/feeds/api/users/<channel>/uploads")
@video.route("/feeds/api/users/<channel>/playlists")
@video.route("/feeds/api/users/<channel>/subscriptions")
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
            resolved = get_channel_id_from_name(channel)

            if resolved and resolved != "unknown":
                channel = resolved
            else:
                return get.error()

        data = get.fetch(
            f"{config.URL}/api/v1/channels/{channel}/videos?sort_by=newest"
        )

        if not data:
            return get.error()

        videos = data.get("videos", [])

        # honor GData's numeric start-index/max-results pagination by
        # slicing locally, since Invidious only gives us one page at a
        # time with no way to jump to an arbitrary numeric offset.
        # Returning an EMPTY page once we run out is what tells the
        # classic app to stop requesting more pages instead of looping.
        try:
            start_index = max(int(request.args.get("start-index", 1)), 1)
        except (TypeError, ValueError):
            start_index = 1
        try:
            max_results = max(int(request.args.get("max-results", 25)), 1)
        except (TypeError, ValueError):
            max_results = 25

        videos = videos[start_index - 1: start_index - 1 + max_results]

        clean = []

        for vid in videos:
            if vid.get("type") == "parse-error":
                continue

            item = normalize_video(vid)

            print(
                "UPLOAD ITEM:",
                item["title"],
                item["viewCount"],
                item["lengthSeconds"],
                item["published"],
                flush=True
            )

            if item:
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

        return get.template("uploads.jinja2", {
            "data": clean,
            "unix": get.unix,
            "url": request.url_root.rstrip("/"),
            "continuation": data.get("continuation"),
        })

    except Exception as e:
        print("CHANNEL UPLOADS ERROR:", e, flush=True)
        return get.error()

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
            "author": html.escape(snippet.get("videoOwnerChannelTitle", "Unknown")),
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
                data = fetch_personal_feed(access_token, browse_id, limit=15)
            except Exception as e:
                print("PERSONALIZED FEED ERROR:", feed_name, e, flush=True)
                data = []

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
            "author": html.escape(
                vid.get("author")
                or vid.get("uploader")
                or vid.get("channel")
                or "Unknown"
            ),
            "authorId": (
                vid.get("authorId")
                or vid.get("uploader_id")
                or vid.get("channel_id")
                or get_channel_id_from_name(vid.get("author") or vid.get("uploader") or vid.get("channel"))
                or "unknown"
            ),
            "viewCount": view_count,
            "lengthSeconds": int(vid.get("lengthSeconds") or vid.get("duration") or 0),
            "published": int(vid.get("published") or vid.get("timestamp") or 0),
            "description": html.escape((vid.get("description") or "")[:120])
        }

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

        print("UPLOADER ID:", item["author"], item["authorId"], flush=True)

        item = apply_cached_metadata(item)
        data.append(item)

        if len(data) >= 30:
            random.shuffle(data)
            return enrich_view_counts(data)

    random.shuffle(data)
    return enrich_view_counts(data)

channel_id_cache = {}

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

    search_query = "popular videos"

    if popular == "most_viewed":
        data = get_most_viewed_from_playlist()
    else:
        data = get_featured_from_hourly_playlists()
    
    print("POPULAR:", popular)
    print("QUERY:", search_query)
    
    import time, html

    global featured_cache, featured_cache_time

    now = time.time()
    cache_key = f"{popular}_{regioncode}"

    if cache_key in featured_cache and now - featured_cache_time.get(cache_key, 0) < 21600:
        print("FEATURED CACHE HIT:", cache_key)
        data = [
            apply_cached_metadata(item)
            for item in data
        ]

        featured_cache[cache_key] = data
    else:
        print("FEATURED CACHE MISS:", cache_key)

        if popular == "most_viewed":
            data = get_most_viewed_from_playlist()
        else:
            data = get_featured_from_hourly_playlists()

        print("FEATURED COUNT:", len(data))

        featured_cache[cache_key] = data
        featured_cache_time[cache_key] = now

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

        for line in result.stdout.splitlines():
            try:
                vid = json.loads(line)
                item = normalize_video(vid)

                if item:
                    data.append(item)

            except Exception as e:
                print("MOST VIEWED PARSE ERROR:", e)

    random.shuffle(data)
    return data[:15]

@video.route("/feeds/api/most_viewed")
@video.route("/<int:res>/feeds/api/most_viewed")
def most_viewed(res=''):

    import subprocess, json, html

    print("MOST VIEWED ROUTE HIT")

    # Clamp res
    if type(res) == int:
        res = min(max(res, 144), config.RESMAX)

    url = request.url_root + str(res)

    import time

    cache_key = "most_viewed"
    now = time.time()

    if cache_key in featured_cache and now - featured_cache_time.get(cache_key, 0) < 1800:
        print("MOST VIEWED CACHE HIT")
        data = featured_cache[cache_key]
    else:
        print("MOST VIEWED CACHE MISS")

        result = subprocess.run(
            ["yt-dlp", "ytsearch15:viral trending popular videos", "--dump-json", "--no-playlist"],
            capture_output=True,
            text=True
        )

        data = []

        for line in result.stdout.splitlines():
            try:
                vid = json.loads(line)
                if not vid.get("id"):
                    continue

                item = {
                    "title": html.escape(vid.get("title") or "Untitled"),
                    "videoId": vid.get("id"),
                    "author": html.escape(vid.get("uploader") or "Unknown"),
                    "authorId": vid.get("channel_id") or vid.get("uploader_id") or get_channel_id_from_name(vid.get("author") or vid.get("uploader") or vid.get("channel")) or "unknown",
                    "viewCount": int(vid.get("view_count") or 0),
                    "lengthSeconds": int(vid.get("duration") or 0),
                    "published": int(vid.get("timestamp") or 0),
                    "description": html.escape((vid.get("description") or "")[:120])
                }

                print("UPLOADER ID:", item["author"], item["authorId"], flush=True)

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

                item = apply_cached_metadata(item)
                data.append(item)

            except Exception as e:
                print("MOST VIEWED ERROR:", e)

        featured_cache[cache_key] = data
        featured_cache_time[cache_key] = now

    if url[-1] == '/':
        url = url[:-1]

    print("MOST VIEWED COUNT:", len(data))

    prompt = build_login_prompt(request)
    if prompt:
        data = [prompt] + data

    user_agent = request.headers.get('User-Agent', '').lower()

    import time
    if data:
        offset = int(time.time()) % len(data)
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

    return get.template("batch_videos.jinja2", {
        "data": clean,
        "unix": get.unix,
        "url": url,
    })

@video.route("/feeds/api/videos/batch", methods=["POST"])
@video.route("/<int:res>/feeds/api/videos/batch", methods=["POST"])
def videos_batch(res=''):
    """GData batch query — used by History. The app keeps its own local
    list of watched video IDs on-device and POSTs them here as a batch
    <feed> of <entry><id>...</id></entry> elements, expecting metadata
    for each back in one response."""
    return _handle_batch_request(res)

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
        # feed rather than a search. Serve trending videos instead of
        # erroring, using the same safe content-src pattern as Featured.
        data = get.fetch(f"{config.URL}/api/v1/trending?region=US")
        if not data:
            return get.error()

        clean = []
        for vid in data:
            # classic YouTube can't play live streams — skip them
            if vid.get("liveNow") or vid.get("isLive") or vid.get("isUpcoming"):
                continue
            item = normalize_video(vid)
            if item:
                clean.append(item)

        # trending has the same missing-stats gap Top Rated/Related/
        # Favorites had — this is what was actually causing the 0 views
        # and 00:00 duration, not (only) live streams
        clean = enrich_view_counts(clean)

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
    time = request.args.get('time')
    if time in helpers.valid_search_time:
        query += f'&date={helpers.valid_search_time[time]}'

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

            if vid.get("liveNow") or vid.get("isLive"):
                continue

            
            print("ADDING:", title, vid_id, duration)

            published = vid.get("published") or vid.get("timestamp") or 0

            if not published and vid.get("publishedText"):
                published = 0

            item = {
                "title": html.escape(str(title)),
                "videoId": vid_id,
                "author": html.escape(str(author)),
                "authorId": vid.get("authorId") or vid.get("uploader_id") or vid.get("channel_id") or get_channel_id_from_name(vid.get("author") or vid.get("uploader") or vid.get("channel")) or  "unknown",
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

        # Download
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
        t2 = time.time()
        
        # Convert (iPhone 3G safe)
        subprocess.run([
            "ffmpeg",
            "-i", temp_input,
            "-vf", "scale=320:240",
            "-c:v", "libx264",
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

        print("FFMPEG SECONDS:", time.time() - t2)

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
                "author": html.escape(str(vid.get("author") or "Unknown")),
                "authorId": vid.get("authorId") or get_channel_id_from_name(vid.get("author") or vid.get("uploader") or vid.get("channel")) or "unknown",
                "viewCount": safe_int(vid.get("viewCount") or 0),
                "lengthSeconds": safe_int(vid.get("lengthSeconds") or 0),
                "published": safe_published(vid.get("published") or 0),
                "description": html.escape(str(vid.get("description") or "")[:120])
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