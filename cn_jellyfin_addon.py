"""
mitmproxy addon: CN Video 1.2.1 fed from a Jellyfin server.

Run from the folder holding this file and app_config_v4.xml:
    mitmdump --listen-port 8888 -s cn_jellyfin_addon.py

Jellyfin series become "shows", episodes become the episode lists, the schedule is
generated from your library, and video playback uses Jellyfin's HLS transcode.
"""
import json
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse, parse_qs, urljoin

from mitmproxy import http

# ------------------------------------------------------------------ SETTINGS
JELLYFIN_URL = "http://you_ip:8096"   # <-- your Jellyfin address, no trailing slash
API_KEY      = "you_api_key"      # Dashboard > Advanced > API Keys
USER_ID      = "you_user_id"                              # optional; Dashboard > Users > click user, copy userId from the page address
CONFIG_FILE  = "app_config_v4.xml"
CONFIG_FILE_2 = "app_config_2.xml"             # config for app version 2.0.x (it asks for a different address)
MAX_EPISODES = 20                              # size of the "Most Recent" list
CACHE_SECONDS = 120
SCHEDULE_SOURCE = "manual"                     # "manual" (my_schedule.txt), "tvmaze", "xmltv" (a guide file you generate), or "jellyfin" (made up from your library)
SCHEDULE_FILE = "my_schedule.txt"              # for SCHEDULE_SOURCE = "manual": your weekly schedule, one line per slot
V2_EPISODE_TYPE = "CLI-EXT"                    # app 2.0: "CLI-EXT" = shown as a clip (no TV-provider login needed), "TVE" = full episode (needs login)
V2_MODE = "full"                              # app 2.0 test: "empty" (no shows), "one" (first show only), "full" (all shows)
V2_FEATURED = True                             # app 2.0: list featured shows (and mark them featured)
V2_SEASONS = True                              # app 2.0: include tveepisodes/clips with season entries for each show
V2_PAD_SEASONS = True                          # app 2.0: always list at least two <season> entries so it sees a list, not a single item
ENABLE_AD_BREAKS = True                        # play bumpers/ads from the two Jellyfin libraries below
BUMPERS_LIBRARY = "Bumpers"                    # Jellyfin library name that holds the bumper clips
ADS_LIBRARY = "Ads"                            # Jellyfin library name that holds the ad clips (one is picked at random per break)
START_BUMPER = "Start.mp4"                     # plays before the episode
BREAK_IN_BUMPER = "AdTransitionFirst.mp4"      # plays when the break starts
BREAK_OUT_BUMPER = "AdTransitionLast.mp4"      # plays when the break ends
MIN_BREAK_SECONDS = 600                        # videos shorter than this get no mid-episode break
SEGMENT_SECONDS = 6                            # Jellyfin's HLS segment length; the break point is rounded to this
LISTEN_PORT = 8080                             # this script also answers directly on this port (in case the phone bypasses the proxy)
SCHEDULE_HOURS = ("06:00", "17:00")            # manual schedule only: show entries from the first time up to (not including) the second. Use None to show the whole day.
SCHEDULE_NETWORKS = ["Cartoon Network"]        # TVmaze network names to include (add "Adult Swim" if you want it)
XMLTV_FILE = "xmltv.xml"                       # for SCHEDULE_SOURCE = "xmltv": path to the guide file (relative to this script)
XMLTV_CHANNEL = "cartoon network"              # channel name to pick out of the XMLTV file (case-insensitive, partial match)
FALLBACK_TO_LIBRARY = False                    # True = if the real source has nothing, show a made-up library schedule instead
# ---------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
_cache = {}


ADDON_VERSION = "v13 (app 2.0: episodes on cnvideosvc2, games plist, player files, MVPD config)"
LOG_FILE = os.path.join(HERE, "cn_log.txt")


def log(msg):
    line = "[cn-jellyfin] " + str(msg)
    print(line, flush=True)
    try:   # also keep everything in cn_log.txt next to this file, so nothing is lost to a scrolling window
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(datetime.now().strftime("%H:%M:%S ") + line + "\n")
    except Exception:
        pass


log("addon loaded: " + ADDON_VERSION)


def jf_get(path, **params):
    nouser = params.pop("_nouser", False)
    if USER_ID and not nouser:
        params.setdefault("userId", USER_ID)
    url = "%s%s?%s" % (JELLYFIN_URL, path, urlencode(params))
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    req = urllib.request.Request(url, headers={
        "Authorization": 'MediaBrowser Token="%s", Client="CNVideo", Device="Proxy", DeviceId="cn-video-proxy", Version="1.0"' % API_KEY,
        "Accept": "application/json"})
    if API_KEY.startswith("PASTE_"):
        raise RuntimeError("API_KEY is still the placeholder; edit the settings at the top of cn_jellyfin_addon.py")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as ex:
        try:
            body = ex.read().decode("utf-8", "replace")[:200]
        except Exception:
            body = ""
        log("Jellyfin answered %s for %s%s %s (key hidden) %s" % (ex.code, JELLYFIN_URL, path, sorted(params), body))
        raise
    _cache[url] = (time.time(), data)
    return data


def img(item_id, kind="Primary", width=320):
    width = min(width, 400)          # keep pictures small so the phone's image loader accepts them
    return "%s/Items/%s/Images/%s?maxWidth=%d&format=Jpg&quality=70&ApiKey=%s" % (JELLYFIN_URL, item_id, kind, width, API_KEY)


def esc(s):
    return (str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("\n", " ").replace("\r", " "))


def xml_response(body):
    return http.Response.make(200, body.encode("utf-8"), {"Content-Type": "application/xml; charset=utf-8"})


def series_list():
    items = []
    variants = [
        dict(SortBy="SortName"),
        dict(),
        dict(SortBy="SortName", _nouser=True),
        dict(_nouser=True),
    ]
    for n, extra in enumerate(variants, 1):
        try:
            d = jf_get("/Items", IncludeItemTypes="Series", Recursive="true",
                       Fields="Overview,Genres,OfficialRating", **dict(extra))
            items = d.get("Items", [])
        except Exception as ex:
            log("series query variant %d failed: %r" % (n, ex))
            continue
        if items:
            log("series query variant %d worked: %d series" % (n, len(items)))
            break
        log("series query variant %d returned 0" % n)
    if not items:
        seen = {}
        try:
            d = jf_get("/Items", IncludeItemTypes="Episode", Recursive="true", Limit="1000",
                       Fields="OfficialRating")
            eps = d.get("Items", [])
            for e in eps:
                sid = e.get("SeriesId")
                if sid and sid not in seen:
                    seen[sid] = {"Id": sid, "Name": e.get("SeriesName", ""), "Overview": "",
                                 "Genres": e.get("Genres") or [], "OfficialRating": e.get("OfficialRating")}
            log("fallback: %d episodes seen, %d with a SeriesId, %d distinct series" %
                (len(eps), sum(1 for e in eps if e.get("SeriesId")), len(seen)))
        except Exception as ex:
            log("episode fallback failed too: %r" % (ex,))
        items = sorted(seen.values(), key=lambda x: x["Name"])
    log("series found: %d" % len(items))
    return items


def episodes_for(series_id=None, recent=False, limit=None):
    p = dict(IncludeItemTypes="Episode", Recursive="true", Fields="Overview,Genres,SeasonName,OfficialRating")
    if series_id:
        p["ParentId"] = series_id
        p["SortBy"] = "ParentIndexNumber,IndexNumber"
        if limit:
            p["Limit"] = str(limit)
    if recent:
        p.update(SortBy="DateCreated", SortOrder="Descending", Limit=str(limit or MAX_EPISODES))
    return jf_get("/Items", **p).get("Items", [])


def all_episodes_by_series():
    d = jf_get("/Items", IncludeItemTypes="Episode", Recursive="true", Limit="1000",
               Fields="SeasonName,OfficialRating")
    out = {}
    for e in d.get("Items", []):
        out.setdefault(e.get("SeriesId"), []).append(e)
    return out


def rating_of(item, default="TV-PG"):
    return item.get("OfficialRating") or default


# ------------------------------------------------------------------ BUILDERS
def build_video_config(base):
    rows = []
    for s in series_list():
        genres = s.get("Genres") or []
        rows.append(
            '    <collection id="%s" category="show" name="%s" header1="%s" header2="%s" mobile3imgurl="%s" '
            'thumbnail="%s" description="%s" tunein="%s" rating="%s"/>' % (
                s["Id"], esc(s["Name"]), esc(s["Name"]), esc(img(s["Id"], "Backdrop", 1024)),
                esc(img(s["Id"], "Primary", 640)), esc(img(s["Id"], "Primary", 320)),
                esc(s.get("Overview", "")), esc(", ".join(genres[:2])), esc(rating_of(s))))
    services = ('<services>\n      <getVideoPlaylist url="%s/playlist.xml?src=jf"/>\n'
                '      <getAllEpisodes url="%s/episodes.xml?src=jf"/>\n    </services>' % (base, base))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n  <allcollections>\n    <timestamp value="%d"/>\n%s\n    %s\n'
            '  </allcollections>\n  %s\n</config>\n' % (int(time.time()) // 600, "\n".join(rows), services, services))


def build_video_config_2(base):
    """Video config in the layout app 2.0.x reads (adds tveepisodes/clips with seasons, featuredShows, sponsorLogos)."""
    by_series = {}
    try:
        by_series = all_episodes_by_series()
    except Exception as ex:
        log("could not count episodes per show: %r" % (ex,))
    rows, featured = [], []
    shows = series_list()
    if V2_MODE == "empty":
        shows = []
    elif V2_MODE == "one":
        shows = shows[:1]
    log("video_config_2: mode=%s, %d show(s), featured=%s, seasons=%s, pad=%s" % (V2_MODE, len(shows), V2_FEATURED, V2_SEASONS, V2_PAD_SEASONS))
    for i, s in enumerate(shows):
        genres = s.get("Genres") or []
        eps = by_series.get(s["Id"], [])
        seasons = {}
        for e in eps:
            seasons[e.get("ParentIndexNumber") or 1] = seasons.get(e.get("ParentIndexNumber") or 1, 0) + 1
        if V2_PAD_SEASONS and len(seasons) < 2:
            seasons[max(seasons) + 1 if seasons else 1] = 0
            if len(seasons) < 2:
                seasons[max(seasons) + 1] = 0
        season_xml = "\n".join('        <season number="%s" name="Season %s" items="%d"/>' % (n, n, c) for n, c in sorted(seasons.items()))
        has_primary = bool((s.get("ImageTags") or {}).get("Primary"))
        has_backdrop = bool(s.get("BackdropImageTags"))
        poster640 = img(s["Id"], "Primary", 640)
        poster320 = img(s["Id"], "Primary", 320)
        banner = img(s["Id"], "Backdrop", 1024) if has_backdrop else poster640
        log("show '%s': poster=%s backdrop=%s" % (s.get("Name"), has_primary, has_backdrop))
        is_feat = V2_FEATURED and i < 5
        # In app 2.0 the fields short_title and description are read as ELEMENT TEXT (the app calls
        # objectForXMLNode on them), so they must be child elements, not attributes.
        name = s["Name"] or "Show"
        desc = (s.get("Overview") or "").strip() or name
        text_children = '\n      <short_title>%s</short_title>\n      <description>%s</description>' % (esc(name), esc(desc))
        seasons_xml = ('\n      <tveepisodes totalItems="%d">\n%s\n      </tveepisodes>\n      <clips totalItems="0"/>' % (len(eps), season_xml)) if V2_SEASONS else ""
        rows.append(
            '    <collection id="%s" category="show" name="%s" header1="%s" header2="%s" mobile3imgurl="%s" '
            'thumbnail="%s" tunein="%s" rating="%s" featuredShowImage="%s" allShowImageUrl="%s" '
            'app_freeWheelSectionID="cn_on_demand" featuredShow="%s">%s%s\n    </collection>' % (
                s["Id"], esc(name), esc(name), esc(banner),
                esc(poster640), esc(poster320),
                esc(", ".join(genres[:2])), esc(rating_of(s)), esc(poster640),
                esc(poster320), "yes" if is_feat else "no", text_children, seasons_xml))
        if is_feat:
            featured.append('      <collection id="%s"/>' % s["Id"])
    services = ('<services>\n      <getVideoPlaylist url="%s/playlist.xml?src=jf"/>\n'
                '      <getAllEpisodes url="%s/episodes.xml?src=jf"/>\n    </services>' % (base, base))
    # The app reads featuredShows and sponsorLogos from the TOP level of the file (next to allcollections),
    # while the timestamp, the collections and the services live inside allcollections.
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n  <allcollections>\n    <timestamp value="%d"/>\n%s\n'
            '    %s\n  </allcollections>\n  <featuredShows>\n%s\n  </featuredShows>\n  <sponsorLogos/>\n  %s\n</config>\n'
            % (int(time.time()) // 600, "\n".join(rows), services, "\n".join(featured), services))


def split_point(secs):
    """Where the mid-episode break goes (seconds, a multiple of SEGMENT_SECONDS), or None for short videos."""
    if not ENABLE_AD_BREAKS or secs < MIN_BREAK_SECONDS:
        return None
    return max(SEGMENT_SECONDS, int(secs / 2 / SEGMENT_SECONDS) * SEGMENT_SECONDS)


def build_episodes(series_id, limit=None):
    items = episodes_for(series_id=series_id, limit=limit) if series_id else episodes_for(recent=True, limit=limit)
    rows = []
    for n, e in enumerate(items, 1):
        secs = int((e.get("RunTimeTicks") or 0) / 10000000)
        genre = (e.get("Genres") or [""])[0]
        cut = split_point(secs)
        thumb = esc(img(e["Id"], "Primary", 320))
        if cut:
            segs = ('      <segment id="%s_s1" thumbnailUrl="%s" episodeId="%s" duration="%d"/>\n'
                    '      <segment id="%s_s2" thumbnailUrl="%s" episodeId="%s" duration="%d"/>' % (
                        e["Id"], thumb, e["Id"], cut, e["Id"], thumb, e["Id"], max(1, secs - cut)))
        else:
            segs = '      <segment id="%s_s1" thumbnailUrl="%s" episodeId="%s" duration="%d"/>' % (e["Id"], thumb, e["Id"], secs)
        rows.append(
            '  <episode id="%s" showId="%s" collectionId="%s" collectionTitle="%s" collectionCategoryType="show" '
            'rating="%s" ranking="%d" numberOfViews="0" title="%s" episodeType="episode" epiSeasonNumber="%s" '
            'epiSeasonName="%s" epiRatingImageUrl="" colHeader2ImageUrl="" colRatingImageUrl="" genre="%s">\n'
            '    <segments>\n%s\n    </segments>\n  </episode>' % (
                e["Id"], e.get("SeriesId", ""), e.get("SeriesId", ""), esc(e.get("SeriesName", "")),
                esc(rating_of(e)), n, esc(e.get("Name", "")), e.get("ParentIndexNumber", ""),
                esc(e.get("SeasonName", "")), esc(genre), segs))
    return '<?xml version="1.0" encoding="UTF-8"?>\n<episodes totalItems="%d">\n%s\n</episodes>\n' % (len(rows), "\n".join(rows))


def build_teaser():
    rows = []
    for s in series_list()[:5]:
        rows.append('    <item type="show" assetURL="%s" collectionID="%s" title="%s" description="%s" tvRating="%s" headerURL="%s"/>' % (
            esc(img(s["Id"], "Backdrop", 1024)), s["Id"], esc(s["Name"]), esc(s.get("Overview", "")[:140]),
            esc(rating_of(s)), esc(img(s["Id"], "Primary", 320))))
    return '<?xml version="1.0" encoding="UTF-8"?>\n<config>\n  <items>\n%s\n  </items>\n</config>\n' % "\n".join(rows)


def build_playlist(seg_id, base):
    """The app asks for a playlist per segment. We hand back a link to our own (trimmed) HLS playlist."""
    href = "%s/hls/%s.m3u8" % (base, seg_id)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n  <entry>\n    <param name="mimeType" value="application/x-mpegURL"/>\n'
            '    <ref href="%s"/>\n  </entry>\n</config>\n' % esc(href))


def pick_date(day_of_month):
    if isinstance(day_of_month, datetime):
        return day_of_month.replace(hour=0, minute=0, second=0, microsecond=0)
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    for k in (0, 1, 2, 3, 4, 5, 6, 7, -1, -2):
        d = today + timedelta(days=k)
        if d.day == day_of_month:
            return d
    return today


def build_schedule(day_of_month):
    d = pick_date(day_of_month)
    shows = [s for s in series_list()]
    by_series = all_episodes_by_series()
    rows = []
    if shows:
        for i in range(48):
            h, m = divmod(i * 30, 60)
            s = shows[(i + d.toordinal()) % len(shows)]
            eps = by_series.get(s["Id"]) or []
            e = eps[(d.toordinal() + i) % len(eps)] if eps else {}
            label = d.replace(hour=h, minute=m).strftime("%I:%M %p").lstrip("0")
            rows.append('  <show title="%s" time="%s" showId="%s" episodeID="%s" episodeName="%s" isFeatured="false" blockName="" '
                        'date="%s" military="%02d:%02d" rating="%s" weekday="%s"/>' % (
                            esc(s["Name"]), label, s["Id"], e.get("Id", ""), esc(e.get("Name", "")),
                            d.strftime("%m/%d/%Y"), h, m, esc(rating_of(s)), d.strftime("%A")))
    return '<?xml version="1.0" encoding="UTF-8"?>\n<schedule>\n%s\n</schedule>\n' % "\n".join(rows)


# ------------------------------------------------------------------ REAL SCHEDULE (TVmaze, free, no key)
def tvmaze_day(date_str):
    """Episodes TVmaze lists as airing on US networks on one date."""
    url = "https://api.tvmaze.com/schedule?country=US&date=" + date_str
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": "cn-video-revival/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read().decode("utf-8"))
    _cache[url] = (time.time(), data)
    return data


def build_schedule_tvmaze(day_of_month):
    d = pick_date(day_of_month)
    try:
        mine = {x["Name"].strip().lower(): x for x in series_list()}
    except Exception:
        mine = {}
    wanted = [n.lower() for n in SCHEDULE_NETWORKS]
    found, seen = [], set()
    total, toonish = 0, {}
    for off in (-1, 0, 1):   # TVmaze days are US Eastern; fetch neighbours so the local day is complete
        for ep in tvmaze_day((d + timedelta(days=off)).strftime("%Y-%m-%d")):
            total += 1
            show = ep.get("show") or (ep.get("_embedded") or {}).get("show") or {}
            net = ((show.get("network") or show.get("webChannel") or {}).get("name") or "").lower()
            if "toon" in net or "cartoon" in net or "adult swim" in net:
                toonish[net] = toonish.get(net, 0) + 1
            if net not in wanted:
                continue
            try:
                if ep.get("airstamp"):
                    when = datetime.fromisoformat(ep["airstamp"].replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
                else:
                    when = datetime.strptime("%s %s" % (ep["airdate"], ep.get("airtime") or "00:00"), "%Y-%m-%d %H:%M")
            except Exception:
                continue
            if when.date() != d.date():
                continue
            key = (show.get("id"), ep.get("id"), when)
            if key in seen:
                continue
            seen.add(key)
            found.append((when, show, ep))
    found.sort(key=lambda t: t[0])
    rows = []
    for when, show, ep in found:
        mine_show = mine.get((show.get("name") or "").strip().lower())
        rating = rating_of(mine_show) if mine_show else "TV-Y7"
        rows.append('  <show title="%s" time="%s" showId="%s" episodeID="" episodeName="%s" isFeatured="false" blockName="" '
                    'date="%s" military="%s" rating="%s" weekday="%s"/>' % (
                        esc(show.get("name", "")), when.strftime("%I:%M %p").lstrip("0"),
                        mine_show["Id"] if mine_show else "", esc(ep.get("name", "")),
                        when.strftime("%m/%d/%Y"), when.strftime("%H:%M"), esc(rating), when.strftime("%A")))
    log("tvmaze returned %d US airings over 3 days; cartoon-like networks seen: %s" % (total, toonish or "none"))
    log("tvmaze schedule for %s: %d airings on %s" % (d.strftime("%Y-%m-%d"), len(rows), ", ".join(SCHEDULE_NETWORKS)))
    return '<?xml version="1.0" encoding="UTF-8"?>\n<schedule>\n%s\n</schedule>\n' % "\n".join(rows)


def build_schedule_xmltv(day_of_month):
    import xml.etree.ElementTree as ET
    path = XMLTV_FILE if os.path.isabs(XMLTV_FILE) else os.path.join(HERE, XMLTV_FILE)
    root = ET.parse(path).getroot()
    d = pick_date(day_of_month)
    want = XMLTV_CHANNEL.lower()
    chan_ids = []
    for c in root.findall("channel"):
        names = [(n.text or "").lower() for n in c.findall("display-name")]
        if any(want in n for n in names):
            chan_ids.append(c.get("id"))
    if not chan_ids:
        raise RuntimeError("no channel matching '%s' in %s" % (XMLTV_CHANNEL, XMLTV_FILE))
    chan = chan_ids[0]
    try:
        mine = {x["Name"].strip().lower(): x for x in series_list()}
    except Exception:
        mine = {}
    found = []
    for pr in root.findall("programme"):
        if pr.get("channel") != chan:
            continue
        raw = (pr.get("start") or "").strip()
        try:
            if " " in raw:
                when = datetime.strptime(raw, "%Y%m%d%H%M%S %z").astimezone().replace(tzinfo=None)
            else:
                when = datetime.strptime(raw[:14], "%Y%m%d%H%M%S")
        except Exception:
            continue
        if when.date() != d.date():
            continue
        title = (pr.findtext("title") or "").strip()
        sub = (pr.findtext("sub-title") or "").strip()
        found.append((when, title, sub))
    found.sort(key=lambda t: t[0])
    rows = []
    for when, title, sub in found:
        ms = mine.get(title.lower())
        rows.append('  <show title="%s" time="%s" showId="%s" episodeID="" episodeName="%s" isFeatured="false" blockName="" '
                    'date="%s" military="%s" rating="%s" weekday="%s"/>' % (
                        esc(title), when.strftime("%I:%M %p").lstrip("0"), ms["Id"] if ms else "", esc(sub),
                        when.strftime("%m/%d/%Y"), when.strftime("%H:%M"), esc(rating_of(ms) if ms else "TV-Y7"),
                        when.strftime("%A")))
    log("xmltv schedule for %s: %d programmes on channel id %s" % (d.strftime("%Y-%m-%d"), len(rows), chan))
    return '<?xml version="1.0" encoding="UTF-8"?>\n<schedule>\n%s\n</schedule>\n' % "\n".join(rows)


# ------------------------------------------------------------------ HAND-MADE WEEKLY SCHEDULE
_DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_LINE_RE = re.compile(r"^(?P<days>\S+)\s+(?P<time>\d{1,2}:\d{2}(?:\s?[AaPp][Mm])?)\s+(?P<title>.+?)(?:\s*\|\s*(?P<ep>.*))?$")


def _parse_days(token):
    t = token.strip().lower().rstrip(",")
    if t in ("daily", "every", "everyday", "all"):
        return set(range(7))
    if t in ("weekdays", "weekday"):
        return set(range(5))
    if t in ("weekends", "weekend"):
        return {5, 6}
    out = set()
    for part in t.split(","):
        part = part.strip()
        if "-" in part:
            a, b = [x.strip()[:3] for x in part.split("-", 1)]
            i, j = _DAY_NAMES[a], _DAY_NAMES[b]
            k = i
            while True:
                out.add(k)
                if k == j:
                    break
                k = (k + 1) % 7
        elif part:
            out.add(_DAY_NAMES[part[:3]])
    return out


def _parse_time(txt):
    txt = txt.strip().upper().replace(" ", "")
    for fmt in ("%H:%M", "%I:%M%p"):
        try:
            return datetime.strptime(txt, fmt)
        except ValueError:
            pass
    raise ValueError("bad time %r" % txt)


def _parse_date_token(token):
    """Returns a date for tokens like 2026-11-04 or 11/04/2026, else None."""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(token.strip(), fmt).date()
        except ValueError:
            pass
    return None


def build_schedule_manual(day_of_month):
    path = SCHEDULE_FILE if os.path.isabs(SCHEDULE_FILE) else os.path.join(HERE, SCHEDULE_FILE)
    if not os.path.isfile(path):
        raise FileNotFoundError("my_schedule.txt not found. Put it in this folder: %s" % HERE)
    d = pick_date(day_of_month)
    def norm(t):
        return t.strip().lower().replace("\u2019", "'")
    try:
        mine = {norm(x["Name"]): x for x in series_list()}
    except Exception:
        mine = {}
    dated, weekly = [], []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = _LINE_RE.match(line)
            if not m:
                log("my_schedule.txt line %d not understood: %s" % (n, line))
                continue
            try:
                t = _parse_time(m.group("time"))
                slot = (t.hour, t.minute, m.group("title").strip(), (m.group("ep") or "").strip())
                day_date = _parse_date_token(m.group("days"))
                if day_date is not None:
                    if day_date == d.date():
                        dated.append(slot)
                elif d.weekday() in _parse_days(m.group("days")):
                    weekly.append(slot)
            except Exception as ex:
                log("my_schedule.txt line %d problem: %r" % (n, ex))
    slots = sorted(dated if dated else weekly)   # entries dated for this exact day override the weekly lines
    if SCHEDULE_HOURS:
        lo = _parse_time(SCHEDULE_HOURS[0]); hi = _parse_time(SCHEDULE_HOURS[1])
        lo_m, hi_m = lo.hour * 60 + lo.minute, hi.hour * 60 + hi.minute
        slots = [x for x in slots if lo_m <= x[0] * 60 + x[1] < hi_m]
    rows = []
    for h, mi, title, ep in slots:
        when = d.replace(hour=h, minute=mi)
        ms = mine.get(norm(title))
        rows.append('  <show title="%s" time="%s" showId="%s" episodeID="" episodeName="%s" isFeatured="false" blockName="" '
                    'date="%s" military="%02d:%02d" rating="%s" weekday="%s"/>' % (
                        esc(title), when.strftime("%I:%M %p").lstrip("0"), ms["Id"] if ms else "", esc(ep),
                        when.strftime("%m/%d/%Y"), h, mi, esc(rating_of(ms) if ms else "TV-Y7"), when.strftime("%A")))
    log("manual schedule for %s (%s): %d slots (%s)" % (d.strftime("%Y-%m-%d"), d.strftime("%A"), len(rows),
                                                        "dated entries" if dated else "weekly lines"))
    return '<?xml version="1.0" encoding="UTF-8"?>\n<schedule>\n%s\n</schedule>\n' % "\n".join(rows)


def schedule_for(day_of_month):
    xml = None
    try:
        if SCHEDULE_SOURCE == "tvmaze":
            xml = build_schedule_tvmaze(day_of_month)
        elif SCHEDULE_SOURCE == "xmltv":
            xml = build_schedule_xmltv(day_of_month)
        elif SCHEDULE_SOURCE == "manual":
            xml = build_schedule_manual(day_of_month)
    except Exception as ex:
        log("%s schedule failed: %r" % (SCHEDULE_SOURCE, ex))
    if xml is not None and "<show " in xml:
        return xml
    if SCHEDULE_SOURCE == "jellyfin" or FALLBACK_TO_LIBRARY:
        log("using the made-up library schedule")
        return build_schedule(day_of_month)
    return xml or '<?xml version="1.0" encoding="UTF-8"?>\n<schedule>\n</schedule>\n'


# ------------------------------------------------------------------ STARTUP SELF-CHECK
def _count(label, **params):
    try:
        d = jf_get("/Items", Recursive="true", Limit="0", **params)
        log("  %-28s %s" % (label, d.get("TotalRecordCount", len(d.get("Items", [])))))
    except Exception as ex:
        log("  %-28s FAILED %r" % (label, ex))


def running():
    """Runs once when mitmproxy starts: prints what Jellyfin lets this key see."""
    _start_direct_server()
    log("---- Jellyfin self-check (%s) ----" % JELLYFIN_URL)
    try:
        req = urllib.request.Request(JELLYFIN_URL + "/System/Info/Public")
        info = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
        log("  server: %s, version %s" % (info.get("ServerName"), info.get("Version")))
    except Exception as ex:
        log("  cannot reach Jellyfin: %r" % (ex,))
        return
    try:
        users = jf_get("/Users")
        for u in users:
            log("  user: %-20s id=%s%s" % (u.get("Name"), u.get("Id"), "   <-- USER_ID setting" if u.get("Id") == USER_ID else ""))
        if USER_ID and not any(u.get("Id") == USER_ID for u in users):
            log("  WARNING: USER_ID is not one of the users above")
    except Exception as ex:
        log("  user list FAILED %r" % (ex,))
    try:
        for v in jf_get("/Library/VirtualFolders"):
            log("  library: %-20s type=%s" % (v.get("Name"), v.get("CollectionType")))
    except Exception as ex:
        log("  library list FAILED %r" % (ex,))
    try:
        d = jf_get("/Items", IncludeItemTypes="Episode", Recursive="true", Limit="1")
        it = (d.get("Items") or [None])[0]
        if it:
            keys = ("Name", "Type", "SeriesId", "SeriesName", "SeasonId", "ParentId", "SeasonName", "ParentIndexNumber")
            log("  sample episode: %s" % {k: it.get(k) for k in keys})
        else:
            log("  sample episode: none returned")
    except Exception as ex:
        log("  sample episode FAILED %r" % (ex,))
    if ENABLE_AD_BREAKS:
        for lib, fn in ((BUMPERS_LIBRARY, START_BUMPER), (BUMPERS_LIBRARY, BREAK_IN_BUMPER), (BUMPERS_LIBRARY, BREAK_OUT_BUMPER)):
            try:
                it = find_clip(lib, fn)
                log("  bumper %-22s %s" % (fn, ("found, %ds" % clip_secs(it)) if it else "NOT FOUND"))
            except Exception as ex:
                log("  bumper %-22s FAILED %r" % (fn, ex))
        try:
            ads = library_clips(ADS_LIBRARY)
            log("  ads in '%s': %s" % (ADS_LIBRARY, [os.path.basename(a.get("Path") or "?") for a in ads]))
        except Exception as ex:
            log("  ads library FAILED %r" % (ex,))
    _count("all items")
    _count("series", IncludeItemTypes="Series")
    _count("episodes", IncludeItemTypes="Episode")
    _count("movies", IncludeItemTypes="Movie")
    log("---- end self-check ----")


# ------------------------------------------------------------------ HLS (two-part episodes)
def jf_get_text(path, **params):
    url = "%s%s?%s" % (JELLYFIN_URL, path, urlencode(params))
    return jf_get_text_url(url)


def jf_get_text_url(url):
    req = urllib.request.Request(url, headers={
        "Authorization": 'MediaBrowser Token="%s", Client="CNVideo", Device="Proxy", DeviceId="cn-video-proxy", Version="1.0"' % API_KEY})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace"), r.geturl()


def item_runtime_secs(item_id):
    d = jf_get("/Items", Ids=item_id, Fields="RunTimeTicks")
    items = d.get("Items", [])
    return int((items[0].get("RunTimeTicks") or 0) / 10000000) if items else 0


def hls_media_playlist(seg_id):
    """A VOD media playlist for one part of an episode, with absolute Jellyfin segment URLs."""
    m = re.fullmatch(r"([0-9a-fA-F]{32})(?:_s(\d))?", seg_id)
    if not m:
        raise ValueError("unexpected segment id %r" % seg_id)
    item_id, part = m.group(1), int(m.group(2) or 1)
    cut = split_point(item_runtime_secs(item_id))
    start_s = cut if (cut and part == 2) else 0
    master, master_url = jf_get_text(
        "/Videos/%s/master.m3u8" % item_id, MediaSourceId=item_id, VideoCodec="h264", AudioCodec="aac",
        SegmentContainer="ts", TranscodingMaxAudioChannels="2", MaxWidth="854", VideoBitrate="1200000",
        AudioBitrate="128000", StartTimeTicks=str(start_s * 10000000), ApiKey=API_KEY)
    variant = next((ln.strip() for ln in master.splitlines() if ln.strip() and not ln.startswith("#")), None)
    if not variant:
        raise RuntimeError("Jellyfin master playlist had no variant")
    text, variant_url = jf_get_text_url(urljoin(master_url, variant))
    keep_segments = (cut // SEGMENT_SECONDS) if (cut and part == 1) else None
    out, seg_count, expect_uri = [], 0, False
    for ln in text.splitlines():
        t = ln.strip()
        if not t:
            continue
        if t.startswith("#EXT-X-ENDLIST"):
            continue
        if t.startswith("#"):
            out.append(t)
            expect_uri = t.startswith("#EXTINF")
            continue
        if keep_segments is not None and seg_count >= keep_segments:
            out.pop()            # drop the #EXTINF line that belongs to the segment we are cutting
            continue
        absu = urljoin(variant_url, t)
        if "ApiKey=" not in absu and "api_key=" not in absu:
            absu += ("&" if "?" in absu else "?") + "ApiKey=" + API_KEY
        out.append(absu)
        seg_count += 1
    out.append("#EXT-X-ENDLIST")
    log("hls %s: part %d, %d segments%s" % (seg_id, part, seg_count, " (cut at %ds)" % cut if cut else ""))
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ ADS AND BUMPERS (FreeWheel-style ad response)
_lib_ids = {}


def _library_id(name):
    if name not in _lib_ids:
        for v in jf_get("/Library/VirtualFolders"):
            if (v.get("Name") or "").lower() == name.lower():
                _lib_ids[name] = v.get("ItemId")
    return _lib_ids.get(name)


def library_clips(lib_name):
    lib = _library_id(lib_name)
    if not lib:
        log("library '%s' not found in Jellyfin" % lib_name)
        return []
    d = jf_get("/Items", ParentId=lib, Recursive="true", MediaTypes="Video", Fields="Path,RunTimeTicks")
    return d.get("Items", [])


def find_clip(lib_name, filename):
    for it in library_clips(lib_name):
        if os.path.basename((it.get("Path") or "").replace("\\", "/")).lower() == filename.lower():
            return it
    log("clip %s not found in library '%s'" % (filename, lib_name))
    return None


def clip_url(it, base=""):
    if base:
        return "%s/clip/%s.mp4" % (base, it["Id"])      # served by this script (with logging), which relays Jellyfin's file
    return "%s/Videos/%s/stream.mp4?static=true&MediaSourceId=%s&ApiKey=%s" % (JELLYFIN_URL, it["Id"], it["Id"], API_KEY)


def clip_secs(it):
    return max(1, int(round((it.get("RunTimeTicks") or 0) / 10000000)))


def clips_for_part(part):
    if part == 1:
        seq = [find_clip(BUMPERS_LIBRARY, START_BUMPER)]
    else:
        ads = library_clips(ADS_LIBRARY)
        seq = [find_clip(BUMPERS_LIBRARY, BREAK_IN_BUMPER), random.choice(ads) if ads else None,
               find_clip(BUMPERS_LIBRARY, BREAK_OUT_BUMPER)]
    return [c for c in seq if c]


def build_ad_response(q, full_url, body="", base=""):
    """FreeWheel-format adResponse with one PREROLL slot (the app only asks for 'temporal-slot-preroll')."""
    caid = ""
    for k, v in q.items():
        if v and re.fullmatch(r"[0-9a-fA-F]{32}(?:_s\d)?", v[0]):
            caid = v[0]
            break
    if not caid and body:
        mm = re.search(r"[0-9a-fA-F]{32}_s\d", body) or re.search(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])", body)
        if mm:
            caid = mm.group(0)
    m = re.fullmatch(r"[0-9a-fA-F]{32}(?:_s(\d))?", caid)
    part = int(m.group(1) or 1) if m else 1
    log("ad request -> part %d (video asset id %r)" % (part, caid))
    if not m:
        log("could not find the segment id in the ad request. URL: %s" % full_url)
    if body:
        log("ad request body (first 3000 chars): %s" % body[:3000].replace("\n", " "))
    clips = clips_for_part(part) if ENABLE_AD_BREAKS else []
    ads_xml, refs = [], []
    for i, c in enumerate(clips, 1):
        secs, url = clip_secs(c), esc(clip_url(c, base))
        ads_xml.append(
            '    <ad adId="%d" adUnit="preroll">\n      <creatives>\n        <creative creativeId="%d" baseUnit="video" duration="%d">\n'
            '          <creativeRenditions>\n            <creativeRendition creativeRenditionId="%d" preference="0" creativeApi="None" '
            'width="320" height="480" contentType="video/mp4" wrapperType="" wrapperUrl="" baseUnit="video" duration="%d">\n'
            '              <asset name="%s" url="%s" mimeType="video/mp4" contentType="video/mp4" bytes="0"/>\n'
            '            </creativeRendition>\n          </creativeRenditions>\n        </creative>\n      </creatives>\n    </ad>'
            % (i, 1000 + i, secs, 2000 + i, secs, esc(os.path.basename((c.get("Path") or "clip").replace("\\", "/"))), url))
        refs.append('            <adReference adId="%d" placementId="%d" creativeId="%d" creativeRenditionId="%d" replicaId="0"/>' % (i, 3000 + i, 1000 + i, 2000 + i))
    total = sum(clip_secs(c) for c in clips)
    for c in clips:
        log("  handing out clip %s (%ds): %s" % (
            os.path.basename((c.get("Path") or "?").replace("\\", "/")), clip_secs(c), clip_url(c, base)))
    if not clips:
        log("  no clips to hand out; sending an empty ad slot")
    # The <ads> list comes FIRST: the SDK resolves each adReference against ads it has already read,
    # so the slot must come after the ads it points to (this is also the order FreeWheel itself uses).
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<adResponse version="1.0">\n'
            '  <ads>\n%s\n  </ads>\n'
            '  <siteSection id="1" customId="cn_on_demand" fallbackId="0" pageViewRandom="1">\n'
            '    <videoPlayer>\n      <videoAsset id="1" customId="%s" videoPlayRandom="1">\n        <adSlots>\n'
            '          <adSlot customId="temporal-slot-preroll" timePosition="0" timePositionClass="PREROLL" cuePointSequence="0" '
            'maxSlotDuration="%d" minSlotDuration="0">\n            <selectedAds>\n%s\n            </selectedAds>\n          </adSlot>\n'
            '        </adSlots>\n      </videoAsset>\n    </videoPlayer>\n  </siteSection>\n</adResponse>\n'
            % ("\n".join(ads_xml), esc(caid), max(60, total + 30), "\n".join(refs)))



# ------------------------------------------------------------------ APP 2.0: episode list, games, player files
def build_episodes_2(q):
    """Episode list in the layout app 2.0 reads (it asks cnvideosvc2.cartoonnetwork.com, which no longer exists)."""
    cid = (q.get("filterByCollectionId") or [""])[0].strip()
    if not re.fullmatch(r"[0-9a-fA-F]{32}|[0-9a-fA-F-]{36}", cid):
        cid = ""
    def num(key, default):
        v = (q.get(key) or [""])[0]
        return int(v) if v.isdigit() else default
    limit, offset = num("limit", 30), num("offset", 0)
    p = dict(IncludeItemTypes="Episode", Recursive="true", Limit=str(limit), StartIndex=str(offset),
             Fields="Overview,Genres,SeasonName,OfficialRating,PremiereDate,DateCreated")
    if cid:
        p.update(ParentId=cid, SortBy="ParentIndexNumber,IndexNumber")
    else:
        p.update(SortBy="DateCreated", SortOrder="Descending")
    d = jf_get("/Items", **p)
    items = d.get("Items", [])
    total = d.get("TotalRecordCount", len(items))
    rows = []
    for n, e in enumerate(items, offset + 1):
        secs = int((e.get("RunTimeTicks") or 0) / 10000000)
        when = (e.get("PremiereDate") or e.get("DateCreated") or "2012-01-01T12:00:00")[:19]
        try:
            premiere = datetime.strptime(when, "%Y-%m-%dT%H:%M:%S").strftime("%m/%d/%y %I:%M %p").lstrip("0")
        except Exception:
            premiere = "01/01/12 12:00 PM"
        thumb = esc(img(e["Id"], "Primary", 640))
        desc = (e.get("Overview") or "").strip() or e.get("Name") or "Episode"
        rows.append(
            '  <episode id="%s" showId="%s" collectionId="%s" collectionTitle="%s" collectionCategoryType="show" rating="%s" '
            'ranking="%d" numberOfViews="0" title="%s" episodeType="%s" originalPremiereDate="%s" subEpisodeNumber="%s" '
            'epiSeasonNumber="%s" epiSeasonName="%s" epiRatingImageUrl="" thumbnailUrl="%s" colHeader2ImageUrl="" '
            'colRatingImageUrl="" genre="%s" duration="%d" videoThumb640x360="%s">\n'
            '    <description>%s</description>\n'
            '    <segments>\n      <segment id="%s" thumbnailUrl="%s" episodeId="%s" duration="%d"/>\n    </segments>\n  </episode>' % (
                e["Id"], e.get("SeriesId", ""), e.get("SeriesId", ""), esc(e.get("SeriesName", "")), esc(rating_of(e)),
                n, esc(e.get("Name", "")), V2_EPISODE_TYPE, premiere, e.get("IndexNumber", ""),
                e.get("ParentIndexNumber", 1), esc(e.get("SeasonName", "Season 1")), thumb,
                esc((e.get("Genres") or [""])[0]), secs, thumb, esc(desc), e["Id"], thumb, e["Id"], secs))
    log("2.0 episode list: %d of %d (collection=%s, offset=%d, limit=%d)" % (len(rows), total, cid[:8] or "all", offset, limit))
    return '<?xml version="1.0" encoding="UTF-8"?>\n<episodes totalItems="%d">\n%s\n</episodes>\n' % (total, "\n".join(rows))


def build_games_plist(base):
    """Games list: app 2.0 reads this as an Apple property list (root dict with a 'games' array)."""
    games = [
        ("Asteroid Dash", "AsteroidDash", "Dodge the asteroids!", True),
        ("Karate Clean Up", "KarateCleanUp", "Slice and clean up!", False),
        ("Snowball Fight", "SnowballFight", "Throw snowballs at your friends!", False),
        ("Tower Defense", "TowerDefense", "Defend the tower!", False),
    ]
    items = ""
    for title, cls, sub, feat in games:
        png = "%s/gameimg/%s.png" % (base, cls)
        png_char = "%s/gameimg/%s_char.png" % (base, cls)
        items += ("    <dict>\n"
                  "      <key>title</key><string>%s</string>\n      <key>subtitle</key><string>%s</string>\n"
                  "      <key>description</key><string>%s</string>\n      <key>class</key><string>%s</string>\n"
                  "      <key>imageURL</key><string>%s</string>\n      <key>characterImageURL</key><string>%s</string>\n"
                  "      <key>requiresAuth</key><false/>\n      <key>active</key><true/>\n      <key>featured</key>%s\n"
                  "    </dict>\n" % (esc(title), esc(sub), esc(sub), cls, png, png_char, "<true/>" if feat else "<false/>"))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0">\n<dict>\n  <key>games</key>\n  <array>\n%s'
            '  </array>\n</dict>\n</plist>\n' % items)



def game_image(name):
    """Serve game_images/<name> if you put a picture there; otherwise an original plain colored tile."""
    folder = os.path.join(HERE, "game_images")
    path = os.path.join(folder, os.path.basename(name))
    if os.path.isfile(path):
        ext = os.path.splitext(path)[1].lower()
        ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif"}.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            return 200, ctype, f.read()
    import zlib, struct
    w, h = (320, 180) if "_char" not in name else (160, 160)
    seed = sum(ord(c) for c in name.split(".")[0].replace("_char", ""))
    r, g, b = (60 + seed * 37) % 160 + 60, (80 + seed * 53) % 160 + 60, (100 + seed * 71) % 160 + 60
    rows = b"".join(b"\x00" + bytes((min(255, r + y * 40 // h), min(255, g + y * 40 // h), min(255, b + y * 40 // h))) * w for y in range(h))
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))
    return 200, "image/png", png


def build_dnalab_json(base):
    """DNA Lab config: DNA items to combine, cards you get from pairs, and energy settings (original sample content)."""
    dna = [("d1", "Spark", "Heat"), ("d2", "Frost", "Cold"), ("d3", "Leaf", "Nature"),
           ("d4", "Stone", "Earth"), ("d5", "Wave", "Water"), ("d6", "Breeze", "Air")]
    dna_items = [{"contentId": i, "contentName": name, "title": name + " DNA", "type": "dna", "collected": True,
                  "category": [{"contentName": cat}],
                  "imageUrl": [{"srcUrl": "%s/dnaimg/dna_%s.png" % (base, i)}]} for i, name, cat in dna]
    combos = [("c1", "d1", "d2", "Steam Cloud", "Common"), ("c2", "d1", "d3", "Ember Vine", "Common"),
              ("c3", "d1", "d4", "Lava Rock", "Rare"), ("c4", "d2", "d5", "Ice Wave", "Common"),
              ("c5", "d3", "d4", "Mossy Boulder", "Common"), ("c6", "d3", "d5", "Lily Pad", "Rare"),
              ("c7", "d4", "d6", "Dust Devil", "Rare"), ("c8", "d5", "d6", "Sea Mist", "Epic")]
    cards = [{"contentId": cid, "dnaID1": a, "dnaID2": b, "collected": False, "card_title": title,
              "cardDescription": "A %s card made from two DNA samples." % title.lower(),
              "set": [{"title": "Starter Set"}], "rarity": [{"rarity": rarity}],
              "imageUrl": [{"srcUrl": "%s/dnaimg/card_%s.png" % (base, cid)}]} for cid, a, b, title, rarity in combos]
    return {"dnaDefaultEnergyValue": 10, "costs": [{"hint": 1, "mixSuccess": 1, "mixFailure": 1}],
            "energyValue": [{"ID": "1", "value": 10}, {"ID": "2", "value": 5}],
            "dnaItem": dna_items, "card": cards}

def build_cvp_container(base):
    """Draft of the player's container file (names taken from the app's reader; layout still partly guessed)."""
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<container>\n  <context name="cn_phone">\n'
            '    <player name="cn_phone" instance="cn_phone" config="%s/cvp_config.xml"/>\n  </context>\n'
            '  <context name="cn_tablet">\n    <player name="cn_tablet" instance="cn_tablet" config="%s/cvp_config.xml"/>\n  </context>\n'
            '</container>\n' % (base, base))


def build_cvp_config(base):
    """Draft of the player's config file. data_src tells it where to fetch each video's info file;
    iOSFileSelection is the bitrate it looks for in that file; media_src is added in front of relative file paths."""
    params = ('<param name="data_src" value="%s/cvpdata/${vid}.xml"/>\n'
              '    <param name="iOSFileSelection" value="1200"/>\n'
              '    <param name="media_src" value=""/>\n'
              '    <param name="tokenizer_src" value=""/>' % base)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n  %s\n'
            '  <player name="cn_phone">\n    %s\n  </player>\n'
            '  <player name="cn_tablet">\n    %s\n  </player>\n'
            '</config>\n' % (params, params, params))


def build_cvp_video(video_id):
    """Video info file: the player looks in <files> for a <file bitrate="..."> whose text is the full video URL."""
    secs = item_runtime_secs(video_id) if re.fullmatch(r"[0-9a-fA-F]{32}", video_id) else 0
    href = ("%s/Videos/%s/master.m3u8?MediaSourceId=%s&VideoCodec=h264&AudioCodec=aac&SegmentContainer=ts"
            "&TranscodingMaxAudioChannels=2&MaxWidth=854&VideoBitrate=1200000&AudioBitrate=128000&ApiKey=%s"
            % (JELLYFIN_URL, video_id, video_id, API_KEY))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<video id="%s">\n  <length>%d</length>\n  <files>\n'
            '    <file bitrate="1200">%s</file>\n    <file bitrate="800" fallback="iOS">%s</file>\n  </files>\n</video>\n'
            % (video_id, secs, esc(href), esc(href)))


# ------------------------------------------------------------------ ROUTING
def handle(path, q, base, full_url="", method="GET", body=""):
    """Returns (status, content_type, body_bytes) for URLs this script serves, else None."""
    name = os.path.basename(path)
    xml_t = "application/xml; charset=utf-8"
    try:
        if path.startswith("/ad/") or (method == "POST" and path in ("", "/")):
            return 200, "text/xml; charset=utf-8", build_ad_response(q, full_url, body, base).encode("utf-8")
        if path.startswith("/hls/") and name.endswith(".m3u8"):
            return 200, "application/vnd.apple.mpegurl", hls_media_playlist(name[:-5]).encode("utf-8")
        if name == "video_config_2.xml":
            body = build_video_config_2(base)
        elif name == "video_config.xml":
            body = build_video_config(base)
        elif name == "episodes.xml":
            log("episodes params: %s" % {k: v[0] for k, v in q.items()})
            cid = (q.get("filterByCollectionId") or [""])[0].strip()
            if not re.fullmatch(r"[0-9a-fA-F]{32}|[0-9a-fA-F-]{36}", cid):
                cid = ""          # the app sends "None" (or nothing) when it wants all shows
            lim = None
            for key in ("limitPerPage", "limit", "pageSize"):
                v = (q.get(key) or [""])[0]
                if v.isdigit() and int(v) > 0:
                    lim = int(v)
                    break
            body = build_episodes(cid, lim)
        elif name == "teaser_config.xml":
            body = build_teaser()
        elif name.startswith("dnalab") and name.endswith(".xml"):
            # DNA Lab / My Collection: despite the .xml name, the app decodes this as JSON.
            log("DNA Lab config requested: %s" % name)
            return 200, "application/json", json.dumps(build_dnalab_json(base)).encode("utf-8")
        elif path.startswith("/dnaimg/"):
            return game_image(name)
        elif path.startswith("/gameimg/"):
            return game_image(name)
        elif name == "games.xml" or name == "games.plist":
            log("games list requested")
            return 200, "application/xml; charset=utf-8", build_games_plist(base).encode("utf-8")
        elif name == "cvp_container.xml":
            log("player container requested")
            body = build_cvp_container(base)
        elif name == "cvp_config.xml":
            log("player config requested")
            body = build_cvp_config(base)
        elif path.startswith("/cvpdata"):
            mv = re.search(r"[0-9a-fA-F]{32}", path)
            log("player video info requested: %s" % path)
            if not mv:
                return 404, "text/plain", b"no video id"
            body = build_cvp_video(mv.group(0))
        elif name == "mvpd_config.xml":
            # TV-provider list for the Adobe Pass sign-in step. The app looks for a <brand> whose id equals its
            # requestor id, then <mvpd enabled="true"> entries containing <system id="ios" enabled="true">.
            log("MVPD config requested")
            body = ('<?xml version="1.0" encoding="UTF-8"?>\n<config>\n'
                    '  <brand id="CartoonNetwork">\n'
                    '    <mvpd id="Placeholder" enabled="true">\n'
                    '      <system id="ios" enabled="true" coBranding="false">\n'
                    '        <device id="iPhone"/>\n        <device id="iPad"/>\n'
                    '      </system>\n    </mvpd>\n  </brand>\n</config>\n')
        elif name == "sponsor.png":
            import base64   # a 1x1 transparent PNG so nothing breaks while loading the sponsor logo
            png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
            return 200, "image/png", png
        elif name == "playlist.xml":
            body = build_playlist((q.get("id") or [""])[0], base)
        elif name == "schedule.xml":
            raw = (q.get("day") or [""])[0]
            md = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{2,4})", raw)
            if md:      # app 2.0: a full date, month/day/year (plus a time, which we ignore)
                yr = int(md.group(3)); yr = yr + 2000 if yr < 100 else yr
                day_arg = datetime(yr, int(md.group(1)), int(md.group(2)))
            else:       # app 1.2.1: just the day of the month
                m = re.search(r"\d+", raw)
                day_arg = int(m.group()) if m else datetime.now().day
            body = schedule_for(day_arg)
        elif name.endswith(".xml") and name not in ("app_config.xml",):
            log("UNKNOWN FILE REQUESTED by the app: %s -> sending an empty <config/>" % full_url)
            body = '<?xml version="1.0" encoding="UTF-8"?>\n<config/>\n'
        else:
            return None
        return 200, xml_t, body.encode("utf-8")
    except Exception as ex:
        log("ERROR building %s: %r" % (path, ex))
        return 502, "text/plain", ("Jellyfin error: %r" % ex).encode()


def request(flow: http.HTTPFlow) -> None:
    req = flow.request
    parsed = urlparse(req.url)
    path, q = parsed.path, parse_qs(parsed.query)

    if req.pretty_host.endswith("cartoonnetwork.com") and path.endswith("/tools/xml/app_config.xml"):
        with open(os.path.join(HERE, CONFIG_FILE), "rb") as f:
            flow.response = http.Response.make(200, f.read(), {"Content-Type": "application/xml; charset=utf-8"})
        return
    if req.pretty_host.endswith("cartoonnetwork.com") and path.endswith("/watchandplay/config/app_config.xml"):
        log("app 2.0 config requested: %s" % req.pretty_url)
        with open(os.path.join(HERE, CONFIG_FILE_2), "rb") as f:
            flow.response = http.Response.make(200, f.read(), {"Content-Type": "application/xml; charset=utf-8"})
        return

    if req.pretty_host == "cnvideosvc2.cartoonnetwork.com" and "getAllEpisodes" in path:
        try:
            body = build_episodes_2(q)
            flow.response = http.Response.make(200, body.encode("utf-8"), {"Content-Type": "application/xml; charset=utf-8"})
        except Exception as ex:
            log("ERROR building 2.0 episode list: %r" % (ex,))
            flow.response = http.Response.make(502, b"error", {"Content-Type": "text/plain"})
        return

    if req.port != LISTEN_PORT:
        return
    res = handle(path, q, "http://%s:%d" % (req.pretty_host, LISTEN_PORT), req.pretty_url,
                 req.method, req.content.decode("utf-8", "replace") if req.content else "")
    if res:
        flow.response = http.Response.make(res[0], res[2], {"Content-Type": res[1]})
        log("%s -> %d" % (req.pretty_url[:140], res[0]))


# ------------------------------------------------------------------ DIRECT SERVER (in case the phone bypasses the proxy)
def _start_direct_server():
    import http.server

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.do_GET()

        def _relay_clip(self, item_id):
            """Relay a bumper/ad file from Jellyfin, honouring Range requests, and log what the phone asks for."""
            url = "%s/Videos/%s/stream.mp4?static=true&MediaSourceId=%s&ApiKey=%s" % (JELLYFIN_URL, item_id, item_id, API_KEY)
            hdrs = {"Authorization": 'MediaBrowser Token="%s", Client="CNVideo", Device="Proxy", DeviceId="cn-video-proxy", Version="1.0"' % API_KEY}
            rng = self.headers.get("Range")
            if rng:
                hdrs["Range"] = rng
            sent = 0
            try:
                try:
                    r = urllib.request.urlopen(urllib.request.Request(url, headers=hdrs), timeout=30)
                except urllib.error.HTTPError as ex:
                    log("clip %s: Jellyfin answered %s" % (item_id, ex.code))
                    self.send_response(ex.code)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(r.status)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                for h in ("Content-Length", "Content-Range"):
                    if r.headers.get(h):
                        self.send_header(h, r.headers.get(h))
                self.end_headers()
                if self.command != "HEAD":
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        sent += len(chunk)
                log("clip %s %s range=%s -> %d, %d bytes sent" % (item_id[:8], self.command, rng, r.status, sent))
            except (BrokenPipeError, ConnectionResetError):
                log("clip %s: phone closed the connection after %d bytes (range=%s)" % (item_id[:8], sent, rng))
            except Exception as ex:
                log("clip %s relay failed after %d bytes: %r" % (item_id[:8], sent, ex))

        def do_HEAD(self):
            u = urlparse(self.path)
            if u.path.startswith("/clip/"):
                return self._relay_clip(os.path.basename(u.path).split(".")[0])
            self.send_response(404)
            self.end_headers()

        def do_GET(self):
            u = urlparse(self.path)
            host = self.headers.get("Host") or ("127.0.0.1:%d" % LISTEN_PORT)
            if u.path.startswith("/clip/"):
                return self._relay_clip(os.path.basename(u.path).split(".")[0])
            body = ""
            if self.command == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
            res = handle(u.path, parse_qs(u.query), "http://" + host, "http://" + host + self.path, self.command, body)
            if not res:
                res = (404, "text/plain", b"not found")
            self.send_response(res[0])
            self.send_header("Content-Type", res[1])
            self.send_header("Content-Length", str(len(res[2])))
            self.end_headers()
            self.wfile.write(res[2])
            log("direct %s -> %d" % (self.path[:120], res[0]))

        def log_message(self, *a):
            pass

    try:
        srv = http.server.ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log("direct server listening on port %d" % LISTEN_PORT)
    except Exception as ex:
        log("could not start the direct server on port %d: %r (the proxy route still works)" % (LISTEN_PORT, ex))
