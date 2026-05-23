from modules import get, helpers
from flask import Blueprint, Flask, request, redirect, render_template, Response
import config
from modules.logs import print_with_seperator
from modules import yt
import os
import time
import threading

video = Blueprint("video", __name__)

featured_cache = {}
featured_cache_time = {}

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
                print("VIEWCOUNT RAW:", repr(vid.get("viewCount")))
                print("FULL RELATED ITEM:", vid)
                item = normalize_video(vid)

                if item:
                    results.append(item)

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

def get_playlist_from_invidious(playlist_id):
    url = f"{config.URL}/api/v1/playlists/{playlist_id}"

    try:
        r = requests.get(url, timeout=10)

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
        print("INVIDIOUS FAILED, FALLING BACK TO YT-DLP")

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

        entries = []

        for line in result.stdout.splitlines():
            try:
                vid = json.loads(line)
                entries.append(vid)
            except Exception as e:
                print("VIDEO LINE PARSE ERROR:", e)

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
                or "unknown"
            ),
            "viewCount": int(vid.get("viewCount") or vid.get("view_count") or 0),
            "lengthSeconds": int(vid.get("lengthSeconds") or vid.get("duration") or 0),
            "published": int(vid.get("published") or vid.get("timestamp") or 0),
            "description": html.escape((vid.get("description") or "")[:120])
        }

        item = apply_cached_metadata(item)
        data.append(item)

        if len(data) >= 30:
            random.shuffle(data)
            return data

    random.shuffle(data)
    return data

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
    playlist_id = "PLNc3mV34rpBmy7pp1sXOR75hQ-PewFsfJ"
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
                    "authorId": vid.get("channel_id") or vid.get("uploader_id") or "unknown",
                    "viewCount": int(vid.get("view_count") or 0),
                    "lengthSeconds": int(vid.get("duration") or 0),
                    "published": int(vid.get("timestamp") or 0),
                    "description": html.escape((vid.get("description") or "")[:120])
                }

                item = apply_cached_metadata(item)
                data.append(item)

            except Exception as e:
                print("MOST VIEWED ERROR:", e)

        featured_cache[cache_key] = data
        featured_cache_time[cache_key] = now

    if url[-1] == '/':
        url = url[:-1]

    print("MOST VIEWED COUNT:", len(data))

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
        return get.error()
    
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

            if "shorts" in title.lower():
                continue

            print("ADDING:", title, vid_id, duration)

            published = vid.get("published") or vid.get("timestamp") or 0

            if not published and vid.get("publishedText"):
                published = 0

            item = {
                "title": html.escape(str(title)),
                "videoId": vid_id,
                "author": html.escape(str(author)),
                "authorId": vid.get("authorId") or vid.get("uploader_id") or vid.get("channel_id") or "unknown",
                "viewCount": int(view_count or 0),
                "lengthSeconds": int(duration or 0),
                "published": int(published or 0),
                "publishedText": vid.get("publishedText") or "",
                "description": html.escape(str(vid.get("description") or ""))
            }

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
    import subprocess
    import json
    import os
    import time

    t0 = time.time()

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
                "authorId": vid.get("authorId") or "unknown",
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