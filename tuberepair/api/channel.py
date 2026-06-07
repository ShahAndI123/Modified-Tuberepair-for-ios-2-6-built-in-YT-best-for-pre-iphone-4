from modules import get, helpers, yt
from flask import Blueprint, Flask, request, redirect, render_template
import config
from modules.logs import print_with_seperator
import requests
import html
import xml.etree.ElementTree as ET

channel = Blueprint("channel", __name__)

# get channel info
@channel.route("/feeds/api/channels/<channel_id>")
@channel.route("/<int:res>/feeds/api/channels/<channel_id>")
def search(channel_id, res=''):
    if not channel_id:
        return get.error()
    # Clamp Res
    if type(res) == int:
        res = min(max(res, 144), config.RESMAX)
    
    url = request.url_root + str(res) 

    # Templates have the / at the end, so let's remove it.
    if url[-1] == '/':
        url = url[:-1]

    # fetch from... you can't believe it.
    # TODO: Make this a config setting letting users use innertube or Invidious!
    data = yt.metadata.simple_channel_info(channel_id)
    # Error handling
    if data and 'error' in data:
        return get.error()

    channel_url = data['channel_id']
    channel_name = data['name']
    channel_pic_url = data['profile_picture']
    sub_count = data['subscribers']

    return get.template('channel_info.jinja2',{
        'author': channel_name,
        'author_id': channel_url,
        'channel_pic_url': channel_pic_url,
        'subcount': sub_count,
        'url': url
    })

# search for channels
@channel.route("/feeds/api/channels")
@channel.route("/<int:res>/feeds/api/channels")
def channels(res=''):
    
    # Clamp Res
    if type(res) == int:
        res = min(max(res, 144), config.RESMAX)
    
    url = request.url_root + str(res) 
    query = request.args.get('q')
    current_page, next_page = helpers.process_start_index(request)
    data = get.fetch(f"{config.URL}/api/v1/search?q={query}&type=channel&page={current_page}")

    # Templates have the / at the end, so let's remove it.
    if url[-1] == '/':
        url = url[:-1]

    if not data:
        next_page = None

        # template
    return get.template('search_results_channel.jinja2',{
        'data': data,
        'url': url,
        'next_page': next_page
    })

    #return get.error()
    

@channel.route("/feeds/api/users/<channel_id>/uploads")
@channel.route("/<int:res>/feeds/api/users/<channel_id>/uploads")
def uploads(channel_id, res=''):
    print("UPLOADS ROUTE USED:", channel_id, flush=True)

    if not channel_id:
        return get.error()

    if type(res) == int:
        res = min(max(res, 144), config.RESMAX)

    url = request.url_root + str(res)

    if url[-1] == '/':
        url = url[:-1]

    try:
        rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

        r = requests.get(
            rss_url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10
        )

        print("RSS URL:", rss_url, flush=True)
        print("RSS STATUS:", r.status_code, flush=True)
        print("RSS START:", r.text[:200], flush=True)

        root = ET.fromstring(r.text)

        ns = {
            "atom": "http://www.w3.org/2005/Atom",
            "yt": "http://www.youtube.com/xml/schemas/2015"
        }

        videos = []

        for entry in root.findall("atom:entry", ns):
            videos.append({
                "title": html.escape(entry.findtext("atom:title", "", ns)),
                "videoId": entry.findtext("yt:videoId", "", ns),
                "author": html.escape(entry.findtext("atom:author/atom:name", "Unknown", ns)),
                "authorId": channel_id,
                "viewCount": 0,
                "lengthSeconds": 0,
                "published": 0,
                "description": ""
            })

        print("RSS UPLOAD COUNT:", len(videos), flush=True)

        return get.template('uploads.jinja2', {
            'data': videos,
            'unix': get.unix,
            'continuation': None,
            'url': url
        })

    except Exception as e:
        print("CHANNEL RSS ERROR:", e, flush=True)
        return get.error()
