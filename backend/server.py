"""
Assistant server for the phone app.

The phone does mic input, speech-to-text, GPS, and audio output.
This server only:
  - asks Gemini what the user wants (directions vs. answer)
  - builds a route with Google Maps
  - tracks navigation progress as the phone reports its GPS

Endpoints:
  POST /assist    {"text": "...", "lat": .., "lng": ..}
                  -> decides intent, returns speech + (if directions) a session
  POST /navigate  {"session": "...", "lat": .., "lng": ..}
                  -> returns the next thing to say, or nothing yet
"""

from flask import Flask, request, jsonify
import requests
import re
import math
import json
import uuid
import socket
import threading
import time

app = Flask(__name__)

# ---------------- API KEYS ----------------
MAPS_API_KEY = ""
GEMINI_API_KEY = ""
GEMINI_MODEL = "gemini-2.5-flash"

# ---------------- NAVIGATION SETTINGS ----------------
ADVANCE_THRESHOLD = 30   # metres from a maneuver before announcing the next
ARRIVE_THRESHOLD = 40    # metres from destination = "arrived"
REROUTE_THRESHOLD = 150  # metres off route before re-requesting directions

# ---------------- GEMINI SYSTEM PROMPT ----------------
SYSTEM_PROMPT = """You are a voice assistant for a blind or low-vision person.
Everything you say is read aloud by a speech synthesizer and heard, never seen.

Rules:
- Keep answers very short: one or two spoken sentences. No lists.
- Plain spoken language only. No markdown, asterisks, bullet points, emoji,
  code, abbreviations that sound odd aloud, or web links.
- Be concrete and direct. The person cannot see, so describe things the way
  you would tell them out loud.
- Decide what the person wants:
  * If they want to travel somewhere or asked for directions / a route / how
    to get to a place, set "type" to "directions" and put just the place name
    or address in "destination".
  * If they want to go to the NEAREST something (e.g. "take me to the nearest
    pharmacy", "find the closest bus stop", "nearest Walmart"), set "type" to
    "nearest" and put the kind of place in "destination" (e.g. "pharmacy",
    "Walmart", "bus stop"). Do not put a specific address.
  * If they are asking where they currently are, their current location,
    what street/area they are in, or "where am I" in any phrasing, set
    "type" to "location". Do NOT guess or state any place yourself in
    "speech" for this case -- the system fills in the real location. Set
    "speech" to a brief lead-in like "One moment." only.
  * Otherwise set "type" to "answer" and answer their question helpfully and
    briefly in "speech".
- ORIGIN: normally the route starts from the person's current location, so
  leave "origin" empty. ONLY if the person explicitly states a different
  starting place ("from the library to the pharmacy"), put that starting
  place in "origin". If they don't say a start, "origin" MUST be empty.
- Always include a short "speech" value: what should be spoken to them right
  now. For directions this is a brief confirmation like "Okay, taking you to
  the Dubai Mall."
- FOLLOW-UP: set "needs_reply" to true ONLY when you genuinely cannot
  proceed without more information (e.g. the destination is ambiguous and
  you must ask which one). Do NOT ask for confirmation of things you can
  reasonably infer. Be decisive; this is a mobility aid, not a chat. When
  "needs_reply" is true, "speech" must be the question to ask. In all
  normal cases "needs_reply" is false.
- Use the recent conversation provided to resolve references like "there",
  "that place", "instead", or "how far is it now".
- Never guess a destination you are not given. If a travel request has no
  clear place, set type "answer", needs_reply true, and ask where to go.
- For anything safety-critical that you cannot reliably judge (crossing roads,
  obstacles, medical concerns), say briefly that you cannot reliably help with
  that and suggest they rely on a trusted aid or person.

Reply with ONLY a JSON object, nothing else, in exactly this shape:
{"type": "directions" or "nearest" or "location" or "answer", "destination": "<place or kind-of-place, or empty>", "origin": "<start place, or empty>", "needs_reply": true or false, "speech": "<what to say now>"}"""

# ---------------- SESSION STORE ----------------
sessions = {}
sessions_lock = threading.Lock()

# Which session the ESP32 LCD should display. Set when navigation
# starts, cleared on arrival. There's only ever one active at a time
# on this single-user device.
current_session_id = None
current_session_lock = threading.Lock()

# ---------------- CONVERSATION HISTORY ----------------
# Per-client (keyed by IP for this single-user device). In memory only,
# resets when the server restarts. Bounded to last 6 turns in /assist.
history_store = {}
history_lock = threading.Lock()

# ---------------- BUTTON STATE (ESP32 -> phone) ----------------
# The ESP32 increments press_count via /press. The phone long-polls /wait
# with the last count it saw; the request is released as soon as the count
# advances (or after a timeout, so connections never hang forever).
press_count = 0
press_cond = threading.Condition()
WAIT_TIMEOUT = 25  # seconds a /wait request blocks before returning empty

# ---------------- HELPERS ----------------
def clean_html(text):
    return re.sub('<[^<]+?>', '', text)


def haversine(lat1, lng1, lat2, lng2):
    R = 6371000
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = (math.sin(dp / 2) ** 2 +
         math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


class QuotaError(Exception):
    """Raised when Gemini returns 429 / RESOURCE_EXHAUSTED."""
    pass


class ServiceUnavailable(Exception):
    """Raised when Gemini is transiently unreachable (timeout / 5xx)
    even after one retry."""
    pass


def ask_gemini(user_text, history=None):
    url = (f"https://generativelanguage.googleapis.com/v1beta/"
           f"models/{GEMINI_MODEL}:generateContent")

    # history is a list of {"role": "user"/"model", "text": "..."}
    contents = []
    for h in (history or []):
        contents.append({"role": h["role"],
                          "parts": [{"text": h["text"]}]})
    contents.append({"role": "user", "parts": [{"text": user_text}]})

    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": contents,
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 200,
            "responseMimeType": "application/json"
        }
    }

    # Try at most twice: once, then one retry on a TRANSIENT failure
    # (network timeout, or Google-side 5xx like 503). 429 is NOT retried
    # (it's a real rate limit and a retry just wastes quota).
    attempts = 2
    last_reason = "unknown"
    for attempt in range(attempts):
        try:
            r = requests.post(
                url,
                headers={"Content-Type": "application/json",
                         "x-goog-api-key": GEMINI_API_KEY},
                json=body,
                timeout=30,
            )
        except requests.exceptions.RequestException as e:
            # timeout / connection error -> transient, retry once
            last_reason = f"network: {e}"
            print(f"Gemini network issue (attempt {attempt+1}): {e}")
            if attempt + 1 < attempts:
                time.sleep(2)
                continue
            raise ServiceUnavailable(last_reason)

        # rate limit: do NOT retry, surface immediately
        if r.status_code == 429:
            raise QuotaError("rate limited")

        # Google-side transient (502/503/500 etc): retry once
        if r.status_code >= 500:
            last_reason = f"http {r.status_code}"
            print(f"Gemini {r.status_code} (attempt {attempt+1})")
            if attempt + 1 < attempts:
                time.sleep(2)
                continue
            raise ServiceUnavailable(last_reason)

        # got a non-5xx, non-429 response -> proceed to parse it
        break

    data = r.json()
    print("GEMINI RAW:", data)

    # quota can also show up in the body, not just the status code
    if isinstance(data, dict) and \
            data.get("error", {}).get("status") == "RESOURCE_EXHAUSTED":
        raise QuotaError("rate limited")

    raw = data["candidates"][0]["content"]["parts"][0]["text"]
    cleaned = (raw.strip()
                  .removeprefix("```json").removeprefix("```")
                  .removesuffix("```").strip())
    parsed = json.loads(cleaned)
    return {
        "type": parsed.get("type", "answer"),
        "destination": (parsed.get("destination") or "").strip(),
        "origin": (parsed.get("origin") or "").strip(),
        "needs_reply": bool(parsed.get("needs_reply", False)),
        "speech": (parsed.get("speech") or "").strip()
    }


def fetch_route(lat, lng, destination):
    # mode=walking -> pedestrian paths, footways, no one-way/highway
    # constraints that don't apply on foot. Correct mode for this user.
    url = ("https://maps.googleapis.com/maps/api/directions/json"
           f"?origin={lat},{lng}"
           f"&destination={requests.utils.quote(destination)}"
           f"&mode=walking"
           f"&key={MAPS_API_KEY}")
    data = requests.get(url, timeout=15).json()
    if data.get("status") != "OK":
        raise RuntimeError(data.get("status", "directions request failed"))

    leg = data["routes"][0]["legs"][0]
    steps = [{
        "instruction": clean_html(s["html_instructions"]),
        "distance_text": s["distance"]["text"],
        "end_lat": s["end_location"]["lat"],
        "end_lng": s["end_location"]["lng"],
    } for s in leg["steps"]]

    final = {"lat": leg["end_location"]["lat"],
             "lng": leg["end_location"]["lng"]}
    eta = leg.get("duration", {}).get("text", "")
    return steps, final, eta


def geocode(place):
    """Turn a place name/address into (lat, lng) via Google Geocoding.
    Used for the X->Y case where the start is a named place, not GPS."""
    url = ("https://maps.googleapis.com/maps/api/geocode/json"
           f"?address={requests.utils.quote(place)}&key={MAPS_API_KEY}")
    data = requests.get(url, timeout=15).json()
    if data.get("status") != "OK" or not data.get("results"):
        raise RuntimeError("geocode failed for: " + place)
    loc = data["results"][0]["geometry"]["location"]
    return loc["lat"], loc["lng"]


def reverse_geocode(lat, lng):
    """Turn the phone's real coordinates into a human-readable place
    for 'where am I'. The MODEL never invents this -- it comes straight
    from Google for the actual coordinates the phone reported."""
    url = ("https://maps.googleapis.com/maps/api/geocode/json"
           f"?latlng={lat},{lng}&key={MAPS_API_KEY}")
    data = requests.get(url, timeout=15).json()
    if data.get("status") != "OK" or not data.get("results"):
        raise RuntimeError("reverse geocode failed")
    # results[0] is the most specific (street address); good enough to
    # orient a person aloud.
    return data["results"][0]["formatted_address"]


def find_nearest_place(lat, lng, keyword):
    """Find the nearest place matching a keyword (e.g. 'Walmart',
    'pharmacy', 'bus stop') to the given coordinates. Returns
    (name, plat, plng) of the nearest result, or None. Uses Places
    Nearby Search ranked by distance."""
    try:
        url = ("https://maps.googleapis.com/maps/api/place/nearbysearch/json"
               f"?location={lat},{lng}"
               f"&rankby=distance"
               f"&keyword={requests.utils.quote(keyword)}"
               f"&key={MAPS_API_KEY}")
        data = requests.get(url, timeout=12).json()
        if data.get("status") != "OK":
            print("find_nearest_place status:", data.get("status"),
                  data.get("error_message", ""))
            return None
        for place in (data.get("results") or []):
            # skip permanently-closed places -- routing someone to a
            # closed store is worse than saying we couldn't find one.
            if place.get("business_status") == "CLOSED_PERMANENTLY":
                continue
            loc = place.get("geometry", {}).get("location", {})
            if "lat" in loc and "lng" in loc:
                return (place.get("name", keyword),
                        loc["lat"], loc["lng"])
        return None
    except Exception as e:
        print("find_nearest_place error:", e)
        return None


def nearest_landmark(lat, lng, radius_m=80):
    """Find the closest named point of interest (a building, shop, etc.)
    to the phone's coordinates via Places Nearby Search. Returns the
    place name, or None if nothing close enough is found / the API call
    fails. Non-fatal: 'where am I' should still work without this."""
    try:
        url = ("https://maps.googleapis.com/maps/api/place/nearbysearch/json"
               f"?location={lat},{lng}"
               f"&rankby=distance"
               f"&key={MAPS_API_KEY}")
        data = requests.get(url, timeout=10).json()
        if data.get("status") != "OK":
            print("nearest_landmark status:", data.get("status"),
                  data.get("error_message", ""))
            return None
        results = data.get("results") or []
        if not results:
            return None
        # rankby=distance gives nearest first. Take it -- but only if
        # it's reasonably close (Places will happily return things far
        # away if nothing's near).
        top = results[0]
        loc = top.get("geometry", {}).get("location", {})
        if "lat" in loc and "lng" in loc:
            # crude distance check via the same coords we sent
            # (we don't need haversine precision here -- a sanity cap)
            dlat = (loc["lat"] - lat) * 111000.0
            dlng = (loc["lng"] - lng) * 111000.0
            if (dlat * dlat + dlng * dlng) > (radius_m * radius_m):
                return None
        return top.get("name")
    except Exception as e:
        print("nearest_landmark error:", e)
        return None


# ---------------- ROUTES ----------------
@app.route("/")
def home():
    return "Assistant API running"


@app.route("/nav_step", methods=["GET"])
def nav_step():
    """The ESP32 polls this every few seconds. Returns the current
    destination (what we're navigating to), or {"active": false} if no
    navigation is running. The LCD shows the destination -- cleaner on
    a 16x2 display than truncated turn instructions, and gives judges
    a clear visual of what the device is doing."""
    with current_session_lock:
        sid = current_session_id
    if not sid:
        return jsonify({"active": False})
    with sessions_lock:
        sess = sessions.get(sid)
    if not sess:
        return jsonify({"active": False})
    return jsonify({
        "active": True,
        "destination": sess["destination"]
    })


@app.route("/update", methods=["POST"])
def update():
    """
    The ESP32 button posts here (unchanged Arduino code):
        {"status": "PRESSED", "distance": <cm>}
    A PRESSED status wakes any phone waiting on /wait.
    'distance' is the ultrasonic reading; accepted and ignored for now
    (available here if you later want obstacle alerts).
    """
    global press_count
    data = request.get_json(force=True, silent=True) or {}
    if data.get("status") == "PRESSED":
        with press_cond:
            press_count += 1
            press_cond.notify_all()
        return jsonify({"status": "ok"})
    return jsonify({"status": "ignored"})


@app.route("/wait", methods=["GET"])
def wait():
    """
    The phone long-polls this. It passes ?since=<last count it saw>.
    Returns immediately if a new press happened; otherwise blocks up to
    WAIT_TIMEOUT seconds, then returns pressed=false so the phone re-polls.
    """
    try:
        since = int(request.args.get("since", 0))
    except (TypeError, ValueError):
        since = 0

    with press_cond:
        if press_count == since:
            press_cond.wait(timeout=WAIT_TIMEOUT)
        pressed = press_count != since
        return jsonify({"pressed": pressed, "count": press_count})


@app.route("/assist", methods=["POST"])
def assist():
    global current_session_id
    data = request.get_json(force=True)
    text = (data.get("text") or "").strip()
    lat = data.get("lat")
    lng = data.get("lng")

    if not text:
        return jsonify({"type": "answer", "needs_reply": False,
                        "speech": "I did not catch that. Please try again."})

    # Per-conversation history. Keyed by client IP (single-user device),
    # bounded to the last few turns so prompt tokens stay small and the
    # model doesn't drift. Lives only while the server runs.
    client_id = request.remote_addr or "default"
    with history_lock:
        hist = history_store.setdefault(client_id, [])
        # pass a copy of current history into the model
        hist_snapshot = list(hist)

    try:
        result = ask_gemini(text, history=hist_snapshot)
    except QuotaError:
        print("Gemini quota / rate limit hit")
        return jsonify({
            "type": "answer", "needs_reply": False,
            "speech": "I'm a little busy right now. "
                      "Please try again in a moment."
        })
    except ServiceUnavailable as e:
        print("Gemini service unavailable:", e)
        return jsonify({
            "type": "answer", "needs_reply": False,
            "speech": "The service is briefly unavailable. "
                      "Please try again."
        })
    except Exception as e:
        print("Gemini error:", e)
        return jsonify({
            "type": "answer", "needs_reply": False,
            "speech": "Sorry, I had trouble thinking about that."
        })

    # record this turn in history (bounded)
    def _remember(user_text, model_text):
        with history_lock:
            h = history_store.setdefault(client_id, [])
            h.append({"role": "user", "text": user_text})
            h.append({"role": "model", "text": model_text})
            # keep only the last 6 turns (12 messages)
            del h[:-12]

    def _clear_history():
        # A task completed -> the conversational thread is done. Wipe it
        # so the NEXT request starts clean and stale task context can't
        # bleed into an unrelated new command.
        with history_lock:
            history_store.pop(client_id, None)

    # ----- Where am I (answered ONLY from the phone's real coords) -----
    if result["type"] == "location":
        if lat is None or lng is None:
            return jsonify({
                "type": "answer", "needs_reply": False,
                "speech": "I could not get your location right now."
            })
        try:
            where = reverse_geocode(lat, lng)
        except Exception as e:
            print("Reverse geocode error:", e)
            return jsonify({
                "type": "answer", "needs_reply": False,
                "speech": "I could not determine your location right now."
            })
        # Optional richer orientation: the nearest named landmark.
        # Best-effort -- if Places isn't enabled or returns nothing
        # nearby, we silently fall back to address-only.
        landmark = nearest_landmark(lat, lng)
        # task complete -> clean slate
        _clear_history()
        # Phrased as approximate on purpose: the fused fix is building/
        # street level, not pinpoint, and reverse-geocode may name an
        # adjacent address. For a blind user acting on this, false
        # precision is the real danger -- so we signal uncertainty.
        # When we have a landmark, we say BOTH it and the street so a
        # wrong nearby landmark is caught by the mismatched street.
        if landmark:
            speech = (f"You appear to be near {landmark}, "
                      f"on {where}. This is approximate.")
        else:
            speech = (f"You appear to be near {where}. "
                      f"This is approximate.")
        return jsonify({
            "type": "answer", "needs_reply": False,
            "speech": speech
        })

    # ----- Nearest <category> (e.g. "take me to the nearest pharmacy") -----
    if result["type"] == "nearest" and result["destination"]:
        if lat is None or lng is None:
            return jsonify({
                "type": "answer", "needs_reply": False,
                "speech": "I could not get your location, so I can't find "
                          "the nearest one right now."
            })
        found = find_nearest_place(lat, lng, result["destination"])
        if not found:
            return jsonify({
                "type": "answer", "needs_reply": False,
                "speech": f"I could not find a {result['destination']} "
                          f"nearby."
            })
        place_name, plat, plng = found
        try:
            # route to the FOUND place's exact coordinates (Directions
            # accepts a "lat,lng" destination), not a re-searched name.
            steps, final, eta = fetch_route(lat, lng, f"{plat},{plng}")
        except Exception as e:
            print("Nearest route error:", e)
            return jsonify({"type": "answer", "needs_reply": False,
                            "speech": f"I found {place_name}, but could not "
                                      f"find a walking route there."})
        if not steps:
            return jsonify({"type": "answer", "needs_reply": False,
                            "speech": f"You are already at {place_name}."})

        session_id = uuid.uuid4().hex
        with sessions_lock:
            sessions[session_id] = {
                "destination": place_name,
                "steps": steps,
                "final": final,
                "index": 0
            }
        with current_session_lock:
            current_session_id = session_id

        speech = (f"The nearest is {place_name}. Estimated time {eta}. "
                  f"First, {steps[0]['instruction']}")
        _clear_history()
        return jsonify({
            "type": "directions",
            "needs_reply": False,
            "session": session_id,
            "destination": place_name,
            "speech": speech
        })

    # Plain answer, or a follow-up question the model needs answered
    if result["type"] != "directions" or not result["destination"]:
        _remember(text, result["speech"])
        return jsonify({
            "type": "answer",
            "needs_reply": result["needs_reply"],
            "speech": result["speech"]
        })

    # ----- Directions -----
    # Origin: a named start place (X->Y) overrides GPS. Otherwise GPS.
    origin_lat, origin_lng = lat, lng
    if result["origin"]:
        try:
            origin_lat, origin_lng = geocode(result["origin"])
        except Exception as e:
            print("Origin geocode error:", e)
            return jsonify({
                "type": "answer", "needs_reply": True,
                "speech": f"I could not find {result['origin']}. "
                          f"Where would you like to start from?"
            })

    if origin_lat is None or origin_lng is None:
        return jsonify({
            "type": "answer", "needs_reply": False,
            "speech": "I could not get your location, so I can't "
                      "give directions right now."
        })

    try:
        steps, final, eta = fetch_route(origin_lat, origin_lng,
                                        result["destination"])
    except Exception as e:
        print("Route error:", e)
        return jsonify({"type": "answer", "needs_reply": False,
                        "speech": "Sorry, I could not find a route there."})

    if not steps:
        return jsonify({"type": "answer", "needs_reply": False,
                        "speech": "You are already at your destination."})

    session_id = uuid.uuid4().hex
    with sessions_lock:
        sessions[session_id] = {
            "destination": result["destination"],
            "steps": steps,
            "final": final,
            "index": 0
        }
    # tell the LCD poller "this is the session to display"
    with current_session_lock:
        current_session_id = session_id

    speech = (f"{result['speech']} Estimated time {eta}. "
              f"First, {steps[0]['instruction']}")
    # navigation has started -> this conversational thread is complete.
    # Clear history so the next button press is treated as a fresh
    # request, not a continuation of this one.
    _clear_history()
    return jsonify({
        "type": "directions",
        "needs_reply": False,
        "session": session_id,
        "destination": result["destination"],
        "speech": speech
    })


@app.route("/navigate", methods=["POST"])
def navigate():
    data = request.get_json(force=True)
    session_id = data.get("session")
    lat = data.get("lat")
    lng = data.get("lng")

    with sessions_lock:
        sess = sessions.get(session_id)

    if not sess:
        return jsonify({"speak": None, "finished": True})
    if lat is None or lng is None:
        return jsonify({"speak": None, "finished": False})

    steps = sess["steps"]
    final = sess["final"]
    index = sess["index"]

    # Arrived?
    if haversine(lat, lng, final["lat"], final["lng"]) < ARRIVE_THRESHOLD:
        with sessions_lock:
            sessions.pop(session_id, None)
        # trip finished -> clear this client's conversation history too
        with history_lock:
            history_store.pop(request.remote_addr or "default", None)
        # and stop the LCD from showing this finished session
        global current_session_id
        with current_session_lock:
            if current_session_id == session_id:
                current_session_id = None
        return jsonify({"speak": "You have arrived at your destination.",
                        "finished": True})

    step = steps[index]
    dist_to_maneuver = haversine(lat, lng, step["end_lat"], step["end_lng"])

    # Off route -> recalculate
    nearest = min(haversine(lat, lng, s["end_lat"], s["end_lng"])
                  for s in steps[index:])
    if nearest > REROUTE_THRESHOLD:
        try:
            steps, final, eta = fetch_route(lat, lng, sess["destination"])
            with sessions_lock:
                sessions[session_id].update(
                    {"steps": steps, "final": final, "index": 0})
            return jsonify({
                "speak": f"Recalculating route. {steps[0]['instruction']}",
                "finished": False
            })
        except Exception as e:
            print("Reroute error:", e)
            return jsonify({"speak": None, "finished": False})

    # Close to the maneuver -> announce the next step
    if dist_to_maneuver < ADVANCE_THRESHOLD:
        index += 1
        if index >= len(steps):
            with sessions_lock:
                sessions.pop(session_id, None)
            with current_session_lock:
                if current_session_id == session_id:
                    current_session_id = None
            return jsonify({"speak": "You have arrived at your destination.",
                            "finished": True})
        with sessions_lock:
            sessions[session_id]["index"] = index
        return jsonify({
            "speak": f"In {steps[index]['distance_text']}, "
                     f"{steps[index]['instruction']}",
            "finished": False
        })

    return jsonify({"speak": None, "finished": False})


def _local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def register_mdns(port=5000):
    """Advertise this server as assistant.local / _assistant._tcp so the
    phone can find it without a hardcoded IP."""
    try:
        from zeroconf import Zeroconf, ServiceInfo
        ip = _local_ip()
        info = ServiceInfo(
            "_assistant._tcp.local.",
            "assistant._assistant._tcp.local.",
            addresses=[socket.inet_aton(ip)],
            port=port,
            server="assistant.local.",
        )
        zc = Zeroconf()
        zc.register_service(info)
        print(f"mDNS: advertising assistant.local -> {ip}:{port}")
        return zc  # keep a reference alive
    except Exception as e:
        print("mDNS registration failed:", e)
        return None


if __name__ == "__main__":
    _zc = register_mdns(5000)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
