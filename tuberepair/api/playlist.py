from flask import Blueprint, request, redirect, send_file, render_template, Response
import config
from modules import get, helpers
from jinja2 import Environment, FileSystemLoader
from api.login import extract_device_id, get_valid_access_token, get_logged_in_channel_id
from api.video import normalize_video
import xml.etree.ElementTree as ET
import requests
import html

playlist = Blueprint("playlist", __name__)

# jinja2 path
env = Environment(loader=FileSystemLoader('templates'))

# get playlists
# TODO: get more video info since invidious simplified it.
@playlist.route("/feeds/api/users/<channel_id>/playlists")
@playlist.route("/<int:res>/feeds/api/users/<channel_id>/playlists")
def playlists(channel_id, res=''):

    # Clamp Res
    if type(res) == int:
        res = min(max(res, 144), config.RESMAX)

    url = request.url_root + str(res)
    continuationToken = request.args.get('continuation') and '?continuation=' + request.args.get('continuation') or ''

    is_own_playlists = channel_id == "default"
    access_token = None

    if is_own_playlists:
        device_id = extract_device_id(request)
        access_token = get_valid_access_token(device_id)
        if not access_token:
            if url[-1] == '/':
                url = url[:-1]
            return get.template('channel_playlists.jinja2', {
                'data': [],
                'continuation': None,
                'url': url,
                'channel_id': channel_id
            })

    if url[-1] == '/':
        url = url[:-1]

    if is_own_playlists:
        # use the authenticated API directly for your own playlists —
        # Invidious only has access to PUBLIC YouTube data, so a private
        # or unlisted playlist you just created would never show up
        # there, even though it was created successfully. This was why
        # new playlists appeared to "disappear" after relaunching the app.
        try:
            r = requests.get(
                "https://www.googleapis.com/youtube/v3/playlists",
                params={"part": "snippet,contentDetails", "mine": "true", "maxResults": 25},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            print("PLAYLISTS (mine=true) status:", r.status_code, flush=True)
            if not r.ok:
                print("PLAYLISTS (mine=true) body:", r.text[:400], flush=True)
                return get.template('channel_playlists.jinja2', {
                    'data': [], 'continuation': None, 'url': url, 'channel_id': channel_id
                })
            items = r.json().get("items", [])
        except Exception as e:
            print("PLAYLISTS (mine=true) ERROR:", repr(e), flush=True)
            return get.template('channel_playlists.jinja2', {
                'data': [], 'continuation': None, 'url': url, 'channel_id': channel_id
            })

        clean = []
        for it in items:
            snippet = it.get("snippet", {})
            thumbs = snippet.get("thumbnails", {})
            thumb_url = (
                thumbs.get("high", {}).get("url")
                or thumbs.get("default", {}).get("url")
                or ""
            )
            clean.append({
                "type": "playlist",
                "title": html.escape(snippet.get("title", "Untitled")),
                "playlistId": it.get("id"),
                "playlistThumbnail": thumb_url,
                "author": html.escape(snippet.get("channelTitle", "Unknown")),
                "authorId": snippet.get("channelId", "unknown"),
                "descriptionHtml": html.escape(snippet.get("description", "")),
                "videoCount": it.get("contentDetails", {}).get("itemCount", 0),
            })

        return get.template('channel_playlists.jinja2', {
            'data': clean,
            'continuation': None,
            'url': url,
            'channel_id': channel_id
        })

    try:
        data = get.fetch(f"{config.URL}/api/v1/channels/{channel_id}/playlists{continuationToken}")

        if data:
            return get.template('channel_playlists.jinja2',{
                'data': data['playlists'],
                'continuation': 'continuation' in data and data['continuation'] or None,
                'url': url,
                'channel_id': channel_id
            })
        raise Exception("No Data was returned!")
    except Exception as e:
        print("PLAYLISTS ERROR:", e, flush=True)
        return get.error()


# get playlist's video
# TODO: fix the damn thing
@playlist.route("/feeds/api/playlists/<playlist_id>", methods=["POST"])
@playlist.route("/<int:res>/feeds/api/playlists/<playlist_id>", methods=["POST"])
def add_video_to_playlist(playlist_id, res=''):
    """Add a video to a playlist, backed by YouTube Data API v3's
    playlistItems.insert. This is what create_playlist()'s <link
    rel='edit'> in video.py points to — without a working handler here,
    the app has nowhere to actually submit the video, which is very
    likely why 'add to playlist' was failing with no request even
    firing. UNVERIFIED against a real capture — built against the
    documented historical GData v2 convention (POST body containing
    <id>tag:youtube.com,2008:video:VIDEO_ID</id>, same shape as
    add_favorite in video.py). Test and send the real capture if wrong."""
    device_id = extract_device_id(request)
    access_token = get_valid_access_token(device_id)
    if not access_token:
        return get.error()

    try:
        body = request.get_data(as_text=True)
        root = ET.fromstring(body)
    except Exception as e:
        print("ADD_TO_PLAYLIST PARSE ERROR:", e, flush=True)
        return get.error()

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    id_el = root.find("atom:id", ns)
    if id_el is None:
        id_el = root.find("id")

    if id_el is None or not id_el.text:
        print("ADD_TO_PLAYLIST: no video id found in body", flush=True)
        return get.error()

    video_id = id_el.text.strip().rsplit(":", 1)[-1]

    try:
        r = requests.post(
            "https://www.googleapis.com/youtube/v3/playlistItems",
            params={"part": "snippet"},
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }
            },
            timeout=10,
        )
        print("ADD_TO_PLAYLIST status:", r.status_code, flush=True)
        if not r.ok:
            print("ADD_TO_PLAYLIST body:", r.text[:400], flush=True)
            return get.error()
    except Exception as e:
        print("ADD_TO_PLAYLIST ERROR:", repr(e), flush=True)
        return get.error()

    return Response(
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<entry xmlns='http://www.w3.org/2005/Atom'>"
        f"<id>tag:youtube.com,2008:video:{video_id}</id>"
        "</entry>",
        mimetype="application/atom+xml"
    )

@playlist.route("/feeds/api/playlists/<playlist_id>")
@playlist.route("/<int:res>/feeds/api/playlists/<playlist_id>")
def playlists_video(playlist_id, res=''):
    
    max_results = request.args.get('max-results')
    # TODO: Find out what it wants when this happens.
    # This happens on YouTube 2.0.0, when you load a video from the playlist it add this
    # for the playlist queue
    if max_results and max_results == '0':
        return get.error()
    if playlist_id.strip().lower() == '(null)':
        return get.error()
    # Clamp Res
    if type(res) == int:
        res = min(max(res, 144), config.RESMAX)

    currentPage, next_page = helpers.process_start_index(request)

    query = f'page={currentPage}'

    # Santize and stitch 
    query = query.replace('&', '&amp;')
    
    url = request.url_root + str(res)
    data = get.fetch(f"{config.URL}/api/v1/playlists/{playlist_id}?{query}")
    
    # Templates have the / at the end, so let's remove it.
    if url[-1] == '/':
        url = url[:-1]

    if not data:
        next_page = None

    if data:
        clean = []
        for vid in data['videos']:
            item = normalize_video(vid)
            if item:
                clean.append(item)

        return get.template('playlist_videos.jinja2',{
            'data': clean,
            'unix': get.unix,
            'url': url,
            'next_page': next_page
        })
    
    return get.error()

# Playlist search (v2.0.0)
@playlist.route("/feeds/api/playlists/snippets")
@playlist.route("/<int:res>/feeds/api/playlists/snippets")
def playlists_search(res=''):
    
    # Clamp Res
    if type(res) == int:
        res = min(max(res, 144), config.RESMAX)

    search_keyword = request.args.get('q')

    if not search_keyword:
        return get.error()
    
    currentPage, next_page = helpers.process_start_index(request)
    
    # remove space character
    search_keyword = search_keyword.replace(" ", "%20")

    query = f'q={search_keyword}&type=playlist&page={currentPage}'

    # Santize and stitch 
    query = query.replace('&', '&amp;')
    
    url = request.url_root + str(res)
    data = get.fetch(f"{config.URL}/api/v1/search?{query}")
    
    # Templates have the / at the end, so let's remove it.
    if url[-1] == '/':
        url = url[:-1]

    if not data:
        next_page = None,

    return get.template('channel_playlists.jinja2',{
            'data': data,
            'url': url,
            'next_page': next_page
        })
