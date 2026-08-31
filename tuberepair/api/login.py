import json, threading, requests, html
from pathlib import Path
from flask import Blueprint, request, render_template, jsonify, Response
import config

login = Blueprint("login", __name__)
SESS_PATH = Path("data/tokens.json")
_lock = threading.Lock()

TV_CLIENT_CONTEXT = {
    "client": {
        "clientName": "TVHTML5",
        "clientVersion": "7.20250205.16.00",
        "hl": "en",
        "gl": "US",
    }
}

# -------------------- session storage -------------------- #

def _load():
    if not SESS_PATH.exists():
        return []
    return json.loads(SESS_PATH.read_text())

def _save(sessions):
    SESS_PATH.parent.mkdir(exist_ok=True)
    SESS_PATH.write_text(json.dumps(sessions, indent=2))

# -------------------- token handling -------------------- #

def validate_token(token):
    r = requests.post(
        "https://www.youtube.com/youtubei/v1/guide",
        headers={"Authorization": f"Bearer {token}"},
        json={"context": TV_CLIENT_CONTEXT},
        timeout=10,
    )
    print("VALIDATE_TOKEN status:", r.status_code, flush=True)
    if not r.ok:
        print("VALIDATE_TOKEN body:", r.text[:300], flush=True)
        return False
    params = r.json().get("responseContext", {}).get("serviceTrackingParams", [{}])[0].get("params", [])
    is_valid = any(p.get("key") == "logged_in" and p.get("value") == "1" for p in params)
    print("VALIDATE_TOKEN logged_in check:", is_valid, flush=True)
    return is_valid

def refresh_access_token(refresh_token):
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=10)
    print("REFRESH_TOKEN status:", r.status_code, flush=True)
    if not r.ok:
        print("REFRESH_TOKEN body:", r.text[:300], flush=True)
        return None
    return r.json().get("access_token")

def get_valid_access_token(device_id):
    print("GET_VALID_ACCESS_TOKEN device_id:", device_id, flush=True)
    if not device_id:
        print("GET_VALID_ACCESS_TOKEN: no device_id given", flush=True)
        return None
    with _lock:
        session = next((s for s in _load() if s["device_id"] == device_id and s.get("is_linked")), None)
    if not session:
        print("GET_VALID_ACCESS_TOKEN: no linked session found for this device_id", flush=True)
        return None
    print("GET_VALID_ACCESS_TOKEN: session found, checking access_token", flush=True)
    if session.get("access_token") and validate_token(session["access_token"]):
        print("GET_VALID_ACCESS_TOKEN: existing access_token is valid", flush=True)
        return session["access_token"]
    print("GET_VALID_ACCESS_TOKEN: access_token invalid/missing, trying refresh_token", flush=True)
    if session.get("refresh_token"):
        new_token = refresh_access_token(session["refresh_token"])
        if new_token:
            print("GET_VALID_ACCESS_TOKEN: refresh succeeded", flush=True)
            with _lock:
                sessions = _load()
                for s in sessions:
                    if s["device_id"] == device_id:
                        s["access_token"] = new_token
                _save(sessions)
            return new_token
        else:
            print("GET_VALID_ACCESS_TOKEN: refresh FAILED, marking session unlinked", flush=True)
            with _lock:
                sessions = _load()
                for s in sessions:
                    if s["device_id"] == device_id:
                        s["is_linked"] = False
                _save(sessions)
    else:
        print("GET_VALID_ACCESS_TOKEN: no refresh_token stored in session", flush=True)
    return None

def is_device_linked(device_id):
    if not device_id:
        return False
    with _lock:
        session = next((s for s in _load() if s["device_id"] == device_id), None)
    return bool(session and session.get("is_linked"))

def any_session_linked():
    """Featured/Most Viewed apparently never send Authorization: GoogleLogin,
    so extract_device_id() falls back to X-YouTube-DeviceAuthToken there —
    which turns out to be regenerated on nearly every request, not a stable
    per-device ID. Strict matching can't work for those two screens. Since
    this is a personal single-user server, just check whether *any* account
    is linked at all as a practical stand-in."""
    with _lock:
        return any(s.get("is_linked") for s in _load())

# -------------------- device id extraction -------------------- #

def extract_device_id(req):
    print("EXTRACT_DEVICE_ID PATH:", req.path, flush=True)
    print("EXTRACT_DEVICE_ID QUERY:", dict(req.args), flush=True)
    print("EXTRACT_DEVICE_ID FORM:", dict(req.form), flush=True)
    print("EXTRACT_DEVICE_ID HEADERS:", dict(req.headers), flush=True)
    device_id = req.args.get("device_id")
    if device_id:
        return device_id

    ua = req.headers.get("User-Agent", "")
    if ua.lower().startswith("com.google.ios.youtube/"):
        auth = req.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()

    device_header = req.headers.get("X-GData-Device")
    if device_header:
        prefix = 'device-id="'
        start = device_header.find(prefix)
        if start != -1:
            start += len(prefix)
            end = device_header.find('"', start)
            if end > start:
                return device_header[start:end]

    auth_header = req.headers.get("Authorization", "")
    if auth_header.lower().startswith("googlelogin"):
        parts = auth_header.split(" ", 1)
        if len(parts) == 2:
            kv = parts[1].split("=", 1)
            if len(kv) == 2 and kv[0].strip() == "auth":
                return kv[1].strip()

    classic_header = req.headers.get("X-YouTube-DeviceAuthToken")
    if classic_header:
        return classic_header

    return None

# -------------------- InnerTube browse (personalized data) -------------------- #

def youtubei_browse(access_token, browse_id, params=None):
    payload = {"context": TV_CLIENT_CONTEXT, "browseId": browse_id}
    if params:
        payload["params"] = params
    try:
        r = requests.post(
            "https://www.youtube.com/youtubei/v1/browse",
            headers={"Authorization": f"Bearer {access_token}"},
            json=payload,
            timeout=15,
        )
        print("YOUTUBEI BROWSE status:", browse_id, r.status_code, flush=True)
        if not r.ok:
            print("YOUTUBEI BROWSE FAILED:", browse_id, r.status_code, r.text[:300], flush=True)
            return None
        data = r.json()
        print(f"YOUTUBEI BROWSE raw JSON for {browse_id} (first 4000 chars):", flush=True)
        print(json.dumps(data, indent=2)[:4000], flush=True)
        return data
    except Exception as e:
        print("YOUTUBEI BROWSE ERROR:", browse_id, e, flush=True)
        return None

def extract_video_renderers(node, found=None):
    """Recursively walk an InnerTube JSON blob and collect video renderer dicts."""
    if found is None:
        found = []
    if isinstance(node, dict):
        for key in ("videoRenderer", "gridVideoRenderer", "compactVideoRenderer", "playlistVideoRenderer", "tileRenderer"):
            if key in node:
                found.append(node[key])
        for v in node.values():
            extract_video_renderers(v, found)
    elif isinstance(node, list):
        for item in node:
            extract_video_renderers(item, found)
    return found

def _text_from(obj):
    if not obj:
        return ""
    if "simpleText" in obj:
        return obj["simpleText"]
    if "runs" in obj:
        return "".join(run.get("text", "") for run in obj["runs"])
    return ""

def _parse_duration_text(text):
    if not text:
        return 0
    parts = text.strip().split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return 0
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds

def _tile_renderer_to_item(r):
    """tileRenderer is the TV/living-room-UI shape (used by tvBrowseRenderer
    feeds like FEsubscriptions) — completely different field layout from
    videoRenderer/gridVideoRenderer, so it needs its own extraction path."""
    video_id = r.get("onSelectCommand", {}).get("watchEndpoint", {}).get("videoId")
    if not video_id:
        return None

    tile_meta = r.get("metadata", {}).get("tileMetadataRenderer", {})
    title = _text_from(tile_meta.get("title")) or "Untitled"

    author = "Unknown"
    author_id = "unknown"
    view_count = 0

    # scan every line's items rather than assuming a fixed position —
    # different tiles put the channel name / view count at different
    # indices, so detect by content instead: a line with a navigationEndpoint
    # pointing at a channel is the author line, a line whose text contains
    # digits followed by "view" is the view-count line.
    lines = tile_meta.get("lines", [])
    for line in lines:
        items = line.get("lineRenderer", {}).get("items", [])
        for it in items:
            line_item = it.get("lineItemRenderer", {})
            text_obj = line_item.get("text", {})
            text = _text_from(text_obj)
            if not text:
                continue

            runs = text_obj.get("runs", [])
            browse_id = None
            for run in runs:
                nav = run.get("navigationEndpoint", {})
                bid = nav.get("browseEndpoint", {}).get("browseId")
                if bid:
                    browse_id = bid
                    break

            if browse_id and author_id == "unknown":
                author = text
                author_id = browse_id
                continue

            lowered = text.lower()
            if "view" in lowered and view_count == 0:
                digits = "".join(c for c in text if c.isdigit())
                if digits:
                    view_count = int(digits)
                continue

            # fallback: if nothing has claimed the author slot yet and this
            # line has no view-count pattern, treat it as the channel name
            # even without a navigationEndpoint (some tiles omit it)
            if author == "Unknown" and "view" not in lowered and "ago" not in lowered:
                author = text

    length_seconds = 0
    overlays = r.get("header", {}).get("tileHeaderRenderer", {}).get("thumbnailOverlays", [])
    for overlay in overlays:
        time_status = overlay.get("thumbnailOverlayTimeStatusRenderer")
        if time_status:
            length_seconds = _parse_duration_text(_text_from(time_status.get("text")))

    return {
        "title": html.escape(title),
        "videoId": video_id,
        "author": html.escape(author),
        "authorId": author_id,
        "viewCount": view_count,
        "lengthSeconds": length_seconds,
        "published": 0,
        "publishedText": "",
        "description": "",
    }

def renderer_to_item(r):
    if "onSelectCommand" in r or "tileMetadataRenderer" in r.get("metadata", {}):
        return _tile_renderer_to_item(r)

    video_id = r.get("videoId")
    if not video_id:
        return None

    title = _text_from(r.get("title")) or "Untitled"
    author = _text_from(r.get("shortBylineText")) or _text_from(r.get("ownerText")) or "Unknown"

    author_id = "unknown"
    byline = r.get("shortBylineText") or r.get("ownerText") or {}
    runs = byline.get("runs", [])
    if runs:
        nav = runs[0].get("navigationEndpoint", {})
        author_id = nav.get("browseEndpoint", {}).get("browseId") or "unknown"

    view_text = _text_from(r.get("viewCountText")) or _text_from(r.get("shortViewCountText"))
    view_count = 0
    if view_text:
        digits = "".join(c for c in view_text if c.isdigit())
        view_count = int(digits) if digits else 0

    length_seconds = _parse_duration_text(_text_from(r.get("lengthText")))
    published_text = _text_from(r.get("publishedTimeText"))

    return {
        "title": html.escape(title),
        "videoId": video_id,
        "author": html.escape(author),
        "authorId": author_id,
        "viewCount": view_count,
        "lengthSeconds": length_seconds,
        "published": 0,  # InnerTube only gives relative text here, not epoch
        "publishedText": published_text,
        "description": "",
    }

def fetch_personal_feed(access_token, browse_id, params=None, limit=15):
    result = youtubei_browse(access_token, browse_id, params)
    if not result:
        return []
    items = []
    for r in extract_video_renderers(result):
        item = renderer_to_item(r)
        if item:
            items.append(item)
        if len(items) >= limit:
            break
    return items

def get_logged_in_channel_id(access_token):
    """Resolve the linked account's own channel ID (UC...) using the
    official YouTube Data API v3 'mine=true' lookup — far more reliable
    than guessing InnerTube's undocumented accounts_list JSON shape."""
    try:
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/channels",
            params={"part": "id", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        print("GET_LOGGED_IN_CHANNEL_ID status:", r.status_code, flush=True)
        if not r.ok:
            print("GET_LOGGED_IN_CHANNEL_ID body:", r.text[:500], flush=True)
            return None
        data = r.json()
        items = data.get("items", [])
        if not items:
            print("GET_LOGGED_IN_CHANNEL_ID: no items in response", flush=True)
            return None
        channel_id = items[0].get("id")
        print("GET_LOGGED_IN_CHANNEL_ID resolved:", channel_id, flush=True)
        return channel_id
    except Exception as e:
        print("GET_LOGGED_IN_CHANNEL_ID ERROR:", repr(e), flush=True)
        return None

# -------------------- routes -------------------- #

@login.route("/o/oauth2/programmatic_auth")
@login.route("/<int:res>/o/oauth2/programmatic_auth")
def programmatic_auth_page(res=None):
    device_id = request.args.get("device_id", "unknown")
    return render_template("web/link_device.html", device_id=device_id)

@login.route("/o/oauth2/start_device_flow", methods=["POST"])
def start_device_flow():
    r = requests.post("https://oauth2.googleapis.com/device/code", data={
        "client_id": config.GOOGLE_CLIENT_ID,
        "scope": "https://www.googleapis.com/auth/youtube",
    })
    return jsonify(r.json()), r.status_code

@login.route("/o/oauth2/poll_device_flow", methods=["POST"])
def poll_device_flow():
    device_code = request.json.get("device_code")
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    })
    body = r.json()
    if r.ok:
        return jsonify({"status": "linked", "tokens": body})
    if body.get("error") == "authorization_pending":
        return jsonify({"status": "pending"})
    return jsonify({"status": "error", "detail": body}), 400

@login.route("/link_device_token", methods=["POST"])
def link_device_token():
    body = request.get_json(force=True)
    device_id = body.get("device_id")
    if not device_id:
        return "Missing device_id", 400
    with _lock:
        sessions = _load()
        existing = next((s for s in sessions if s["device_id"] == device_id), None)
        is_linked = validate_token(body.get("access_token", ""))
        data = {
            "device_id": device_id,
            "username": body.get("username", ""),
            "password": body.get("password", ""),
            "access_token": body.get("access_token", ""),
            "refresh_token": body.get("refresh_token", ""),
            "is_linked": is_linked,
        }
        if existing:
            existing.update(data)
        else:
            sessions.append(data)
        _save(sessions)
    return "Device linked"

@login.route("/check_if_username_is_taken")
def check_username():
    username = request.args.get("username", "")
    with _lock:
        taken = any(s["username"] == username for s in _load())
    return jsonify({"Status": taken})

@login.route("/accounts/ClientLogin", methods=["POST"])
@login.route("/youtube/accounts/ClientLogin", methods=["POST"])
def client_login():
    email = request.form.get("Email", "")
    passwd = request.form.get("Passwd", "")
    if not email or not passwd:
        return "You must have a username and password!", 200
    with _lock:
        session = next((s for s in _load() if s["username"] == email and s["password"] == passwd), None)
    if not session:
        return "Your username and password are wrong, or your account isn't linked!!!", 200
    device_id = session["device_id"]
    return Response(f"SID={device_id}\nLSID={device_id}\nAuth={device_id}\n", mimetype="text/plain")
