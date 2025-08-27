import os, math, time, json, random, string, pathlib, logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Tuple
from flask import Flask, request, jsonify, send_from_directory, make_response
import requests
from dotenv import load_dotenv, find_dotenv

# ── Load .env
load_dotenv(find_dotenv())

# ── Resolve static folder robustly
def _pick_static_dir():
    here = pathlib.Path(__file__).parent
    candidates = [here / ".." / "web", here / "web", here / ".." / ".." / "web"]
    for p in candidates:
        if (p / "index.html").exists():
            return str(p.resolve())
    return str((here / ".." / "web").resolve())

STATIC_DIR = _pick_static_dir()

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/")

# (필요시) 다른 포트 프론트용 CORS 허용
try:
    from flask_cors import CORS
    CORS(app, resources={r"/api/*": {"origins": "*"}})
except Exception:
    pass

# 개발 중 캐시 끔(브라우저가 예전 JS를 붙잡는 이슈 방지)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

# ── Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("meetpoint")

# ── ENV
KAKAO_REST_KEY = (os.getenv("KAKAO_REST_KEY") or "").strip()
GOOGLE_API_KEY = (os.getenv("GOOGLE_PLACES_KEY") or os.getenv("GOOGLE_MAPS_KEY") or "").strip()
KAKAO_JS_KEY   = (os.getenv("KAKAO_JS_KEY") or "").strip()

# ── Storage
ROOMS: Dict[str, Dict] = {}
ROOMS_PATH = pathlib.Path(__file__).with_name("rooms.json")

def _now_ms(): return int(time.time() * 1000)
def _gen_code(n=6):
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(random.choice(chars) for _ in range(n))
def _gen_pid():
    return "P" + "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(6))

def _cleanup_and_save():
    now = _now_ms()
    expired = [c for c, r in list(ROOMS.items()) if r.get("expires_at", now) <= now]
    for c in expired:
        ROOMS.pop(c, None)
    try:
        ROOMS_PATH.write_text(json.dumps(ROOMS, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.warning("rooms save failed: %s", e)

def _load_rooms():
    global ROOMS
    try:
        data = json.loads(ROOMS_PATH.read_text(encoding="utf-8"))
        now = _now_ms()
        ROOMS = {c: r for c, r in data.items() if r.get("expires_at", now) > now}
        log.info("rooms loaded: %d", len(ROOMS))
    except Exception:
        ROOMS = {}

_load_rooms()

# ── Geo/Time utils
R_EARTH = 6371000.0

def haversine_km(lat1,lng1,lat2,lng2):
    dlat = math.radians(lat2-lat1)
    dlng = math.radians(lng2-lng1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlng/2)**2
    c = 2*math.atan2(math.sqrt(1-a), math.sqrt(a))
    return (R_EARTH*c)/1000.0

def _parse_meeting_time(s: str | None) -> datetime:
    if not s:
        return datetime.now()
    try:
        return datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return datetime.now()

def time_weighted_centroid(points: List[Dict]) -> Dict | None:
    if not points: return None
    speeds = {"car":40.0, "subway":35.0, "bus":20.0, "walk":5.0}  # km/h
    lat0 = sum(p["lat"] for p in points)/len(points)
    lng0 = sum(p["lng"] for p in points)/len(points)
    cos0 = math.cos(math.radians(lat0))
    swx = swy = sw = 0.0
    for p in points:
        v = speeds.get(p.get("mode","car"), 40.0)
        w = 1.0/max(v,1.0)  # 느릴수록(시간 비용↑) 가중치↑
        x = (p["lng"]-lng0)*cos0
        y = (p["lat"]-lat0)
        swx += w*x; swy += w*y; sw += w
    cx = lng0 + (swx/max(sw,1e-9))/max(cos0,1e-9)
    cy = lat0 + (swy/max(sw,1e-9))
    return {"lat":cy, "lng":cx}

# ── Kakao Local
def kakao_category_search(lat,lng,category,radius):
    if not KAKAO_REST_KEY:
        return {"ok":False, "error":"KAKAO_REST_KEY_not_set"}
    url = "https://dapi.kakao.com/v2/local/search/category.json"
    params = {
        "category_group_code": category,
        "y": lat, "x": lng,
        "radius": max(100, min(int(radius or 2000), 20000)),
        "size": 15, "sort": "distance",
    }
    r = requests.get(url, params=params, headers={"Authorization": f"KakaoAK {KAKAO_REST_KEY}"}, timeout=10)
    if r.status_code != 200:
        return {"ok":False, "error":f"kakao_http_{r.status_code}", "body":r.text}
    data = r.json()
    return {"ok":True, "items": data.get("documents",[]), "count": len(data.get("documents",[]))}

def kakao_keyword_search(lat, lng, query, radius, category_group_code=None):
    if not KAKAO_REST_KEY:
        return {"ok": False, "error": "KAKAO_REST_KEY_not_set"}
    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    params = {
        "query": query, "x": lng, "y": lat,
        "radius": max(100, min(int(radius or 2000), 20000)),
        "size": 15, "sort": "distance",
    }
    if category_group_code:
        params["category_group_code"] = category_group_code
    r = requests.get(url, params=params, headers={"Authorization": f"KakaoAK {KAKAO_REST_KEY}"}, timeout=10)
    if r.status_code != 200:
        return {"ok": False, "error": f"kakao_http_{r.status_code}", "body": r.text}
    data = r.json()
    return {"ok": True, "items": data.get("documents", []), "count": len(data.get("documents", []))}

# ── Google Places / Distance Matrix
def google_type_for(category):
    if category == "BAR": return "bar"
    if category == "CE7": return "cafe"
    return "restaurant"

def google_enrich(name, lat, lng, category):
    if not GOOGLE_API_KEY:
        return {}
    try:
        gtype = google_type_for(category)
        nearby = requests.get(
            "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
            params={"key": GOOGLE_API_KEY, "location": f"{lat},{lng}", "radius": 120, "keyword": name, "type": gtype},
            timeout=10
        ).json()
        candidates = nearby.get("results", [])
        if not candidates:
            return {}
        place_id = candidates[0]["place_id"]
        details = requests.get(
            "https://maps.googleapis.com/maps/api/place/details/json",
            params={
                "key": GOOGLE_API_KEY, "place_id": place_id,
                "fields": "opening_hours,current_opening_hours,formatted_phone_number,international_phone_number,website,photos,url"
            },
            timeout=10
        ).json()
        result = details.get("result", {}) or {}

        photo_url = ""
        photos = result.get("photos") or []
        if photos:
            ref = photos[0].get("photo_reference")
            if ref:
                photo_url = f"https://maps.googleapis.com/maps/api/place/photo?maxwidth=640&photo_reference={ref}&key={GOOGLE_API_KEY}"

        cur = result.get("current_opening_hours") or {}
        reg = result.get("opening_hours") or {}
        weekday_text = cur.get("weekday_text") or reg.get("weekday_text")
        periods = cur.get("periods") or reg.get("periods")
        open_now = None
        if "open_now" in cur: open_now = cur.get("open_now")
        elif "open_now" in reg: open_now = reg.get("open_now")

        return {
            "_open_now": open_now,
            "_weekday_text": weekday_text,
            "_periods": periods,
            "_phone": result.get("formatted_phone_number") or result.get("international_phone_number"),
            "_website": result.get("website"),
            "_photo_url": photo_url
        }
    except Exception:
        return {}

def google_distance_matrix(origins: List[Tuple[float,float]],
                           dest: Tuple[float,float],
                           mode: str,
                           transit_mode: str | None,
                           departure_time_unix: int) -> List[int | None]:
    if not GOOGLE_API_KEY:
        return [None] * len(origins)
    durations: List[int | None] = []
    base_params = {
        "key": GOOGLE_API_KEY,
        "destinations": f"{dest[0]},{dest[1]}",
        "mode": mode,
        "departure_time": departure_time_unix
    }
    if mode == "transit" and transit_mode:
        base_params["transit_mode"] = transit_mode

    def _one_chunk(chunk: List[Tuple[float,float]]):
        params = base_params.copy()
        params["origins"] = "|".join([f"{lat},{lng}" for (lat,lng) in chunk])
        resp = requests.get("https://maps.googleapis.com/maps/api/distancematrix/json", params=params, timeout=10)
        if resp.status_code != 200:
            return [None]*len(chunk)
        data = resp.json()
        rows = data.get("rows", [])
        out: List[int | None] = []
        for row in rows:
            elements = (row or {}).get("elements", [])
            el0 = elements[0] if elements else {}
            if el0.get("status") != "OK":
                out.append(None); continue
            dur = (el0.get("duration_in_traffic") or el0.get("duration") or {}).get("value")
            out.append(int(round((dur or 0)/60)) if dur is not None else None)
        return out

    i = 0
    while i < len(origins):
        chunk = origins[i:i+25]  # API limit
        durations.extend(_one_chunk(chunk))
        i += 25
    return durations

# ── Opening-hours helpers
def _google_day_from_py(py_weekday: int) -> int:
    return (py_weekday + 1) % 7

def _dt_for_google_day(base_date: datetime, gday: int, hhmm: str) -> datetime:
    g_base = _google_day_from_py(base_date.weekday())
    delta_days = gday - g_base
    d = base_date + timedelta(days=delta_days)
    hh = int(hhmm[:2]) if hhmm else 0
    mm = int(hhmm[2:]) if hhmm else 0
    return datetime(d.year, d.month, d.day, hh, mm, 0)

def _minutes_open_after(meeting_dt: datetime, periods: list) -> tuple | None:
    best = None
    for p in periods or []:
        op = p.get("open") or {}
        cl = p.get("close") or {}
        if "day" not in op or "time" not in op:
            continue
        g_open_day = int(op["day"])
        open_dt = _dt_for_google_day(meeting_dt, g_open_day, str(op["time"]))
        if "day" in cl and "time" in cl:
            g_close_day = int(cl["day"])
            close_dt = _dt_for_google_day(meeting_dt, g_close_day, str(cl["time"]))
            if close_dt <= open_dt:  # 밤샘
                close_dt += timedelta(days=1)
        else:
            close_dt = open_dt + timedelta(hours=24)
        if open_dt <= meeting_dt < close_dt:
            mins = int((close_dt - meeting_dt).total_seconds() // 60)
            if (best is None) or (mins > best[0]):
                best = (mins, close_dt)
    return best

# ─────────────────────────────────────────────────────────────────────────────
# ⬇ API ROUTES (정적 서빙보다 위) ⬇
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/api/health", methods=["GET", "HEAD"])
def health():
    payload = {
        "ok": True,
        "ts": _now_ms(),
        "kakao_rest_key": bool(KAKAO_REST_KEY),
        "google_key": bool(GOOGLE_API_KEY),
        "static_dir": STATIC_DIR,
    }
    resp = make_response(jsonify(payload), 200)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp

@app.route("/_health", methods=["GET", "HEAD"])
def _health_alias():
    return health()

@app.route("/api/config")
def config():
    return jsonify({"kakao_js_key": KAKAO_JS_KEY, "google_key_present": bool(GOOGLE_API_KEY)})

# ── Room APIs
@app.route("/api/room/create", methods=["POST"])
def room_create():
    body = request.get_json(silent=True) or {}
    ttl = int(body.get("ttlMinutes") or 120)
    purpose = (body.get("purpose") or "").strip()
    meeting_time = (body.get("meetingTime") or "").strip()

    code = _gen_code()
    while code in ROOMS: code = _gen_code()
    expires_at = _now_ms() + ttl*60*1000
    host_secret = "HS_" + _gen_code(8)

    ROOMS[code] = {
        "code": code,
        "created_at": _now_ms(),
        "expires_at": expires_at,
        "meta": {"purpose": purpose, "meetingTime": meeting_time},
        "participants": {},
        "ver": 0,
        "results": None,
        "host_secret": host_secret,
        "eta": None,
    }
    _cleanup_and_save()

    join_url = request.host_url.rstrip("/") + "/?code=" + code
    log.info("room created code=%s join=%s", code, join_url)
    return jsonify({"ok": True, "code": code, "expiresAt": expires_at,
                    "meta": ROOMS[code]["meta"], "joinUrl": join_url, "hostSecret": host_secret})

@app.route("/api/room/join", methods=["POST"])
def room_join():
    body = request.get_json(silent=True) or {}
    code = (body.get("code") or "").upper()
    nickname = (body.get("nickname") or "익명").strip()
    pid = body.get("pid")

    if code not in ROOMS:
        return jsonify({"ok": False, "error": "room_not_found"}), 404

    if pid and pid in ROOMS[code]["participants"]:
        ROOMS[code]["participants"][pid]["nickname"] = nickname
        ROOMS[code]["ver"] += 1
        _cleanup_and_save()
        return jsonify({"ok": True, "pid": pid})

    pid = _gen_pid()
    ROOMS[code]["participants"][pid] = {"pid": pid, "nickname": nickname, "mode": "car", "lat": None, "lng": None, "updated_at": 0}
    ROOMS[code]["ver"] += 1
    _cleanup_and_save()
    return jsonify({"ok": True, "pid": pid})

@app.route("/api/room/update", methods=["POST"])
def room_update():
    body = request.get_json(silent=True) or {}
    code = (body.get("code") or "").upper()
    pid = body.get("pid")
    if code not in ROOMS:
        return jsonify({"ok": False, "error": "room_not_found"}), 404
    p = ROOMS[code]["participants"].get(pid)
    if not p:
        return jsonify({"ok": False, "error": "participant_not_found"}), 404
    try:
        if body.get("lat") is not None: p["lat"] = float(body.get("lat"))
        if body.get("lng") is not None: p["lng"] = float(body.get("lng"))
    except Exception:
        return jsonify({"ok": False, "error": "bad_latlng"}), 400
    mode = body.get("mode")
    if mode in ("car","bus","subway","walk"): p["mode"] = mode
    p["updated_at"] = _now_ms()
    ROOMS[code]["ver"] += 1
    _cleanup_and_save()
    return jsonify({"ok": True})

@app.route("/api/room/leave", methods=["POST"])
def room_leave():
    body = request.get_json(silent=True) or {}
    code = (body.get("code") or "").upper()
    pid = body.get("pid")
    if code not in ROOMS:
        return jsonify({"ok": False, "error": "room_not_found"}), 404
    ROOMS[code]["participants"].pop(pid, None)
    ROOMS[code]["ver"] += 1
    _cleanup_and_save()
    return jsonify({"ok": True})

@app.route("/api/room/close", methods=["POST"])
def room_close():
    body = request.get_json(silent=True) or {}
    code = (body.get("code") or "").upper()
    host_secret = (body.get("hostSecret") or "").strip()
    if code not in ROOMS:
        return jsonify({"ok": False, "error": "room_not_found"}), 404
    if host_secret != ROOMS[code].get("host_secret"):
        return jsonify({"ok": False, "error": "host_secret_mismatch"}), 403
    ROOMS.pop(code, None)
    _cleanup_and_save()
    return jsonify({"ok": True})

@app.route("/api/room/state")
def room_state():
    code = (request.args.get("code") or "").upper()
    if code not in ROOMS:
        return jsonify({"ok": False, "error": "room_not_found"}), 404
    room = ROOMS[code]
    plist = list(room["participants"].values())
    pts = [{"lat":p["lat"],"lng":p["lng"],"mode":p["mode"]} for p in plist if isinstance(p.get("lat"),(int,float)) and isinstance(p.get("lng"),(int,float))]
    centroid = time_weighted_centroid(pts) if pts else None
    return jsonify({
        "ok": True, "code": code, "meta": room["meta"],
        "participants": plist, "centroid": centroid,
        "ver": room["ver"], "results": room["results"], "eta": room.get("eta")
    })

# ── ETA-midpoint
def _group_modes(participants: List[Dict]):
    g = {"driving": [], "walking": [], "transit_bus": [], "transit_subway": []}
    for idx, p in enumerate(participants):
        mode = p.get("mode", "car")
        lat, lng = p.get("lat"), p.get("lng")
        if not isinstance(lat,(int,float)) or not isinstance(lng,(int,float)):
            continue
        if mode == "car":
            g["driving"].append((idx, lat, lng))
        elif mode == "walk":
            g["walking"].append((idx, lat, lng))
        elif mode == "bus":
            g["transit_bus"].append((idx, lat, lng))
        elif mode == "subway":
            g["transit_subway"].append((idx, lat, lng))
        else:
            g["driving"].append((idx, lat, lng))
    return g

def _etas_for_destination(participants: List[Dict], dest_lat: float, dest_lng: float, depart_unix: int) -> List[int | None]:
    if GOOGLE_API_KEY:
        groups = _group_modes(participants)
        etas: List[int | None] = [None] * len(participants)

        def fill(indices_with_coords, mode, transit_mode=None):
            if not indices_with_coords: return
            origins = [(lat,lng) for (_,lat,lng) in indices_with_coords]
            mins = google_distance_matrix(origins, (dest_lat,dest_lng), mode, transit_mode, depart_unix)
            for (i, _lat, _lng), m in zip(indices_with_coords, mins):
                etas[i] = m

        fill(groups["driving"], "driving", None)
        fill(groups["walking"], "walking", None)
        fill(groups["transit_bus"], "transit", "bus")
        fill(groups["transit_subway"], "transit", "subway")

        # 누락값은 속도기반 보정
        speeds = {"car":40.0, "subway":35.0, "bus":20.0, "walk":5.0}
        for i, e in enumerate(etas):
            if e is None:
                p = participants[i]
                d_km = haversine_km(p["lat"], p["lng"], dest_lat, dest_lng)
                v = speeds.get(p.get("mode","car"), 40.0)
                etas[i] = int(round((d_km / max(v,1e-9)) * 60))
        return etas

    # Google 키 없으면 속도기반
    speeds = {"car":40.0, "subway":35.0, "bus":20.0, "walk":5.0}
    out = []
    for p in participants:
        d_km = haversine_km(p["lat"], p["lng"], dest_lat, dest_lng)
        v = speeds.get(p.get("mode","car"), 40.0)
        out.append(int(round((d_km / max(v,1e-9)) * 60)))
    return out

def _gen_candidates(center_lat: float, center_lng: float, radius_m: int, rings=3, per_ring=16) -> List[Tuple[float,float]]:
    out = [(center_lat, center_lng)]
    if radius_m <= 0:
        return out
    def offset_latlng(lat, lng, d_m, bearing_deg):
        d = d_m / R_EARTH
        br = math.radians(bearing_deg)
        lat1 = math.radians(lat); lng1 = math.radians(lng)
        lat2 = math.asin(math.sin(lat1)*math.cos(d) + math.cos(lat1)*math.sin(d)*math.cos(br))
        lng2 = lng1 + math.atan2(math.sin(br)*math.sin(d)*math.cos(lat1), math.cos(d)-math.sin(lat1)*math.sin(lat2))
        return (math.degrees(lat2), math.degrees(lng2))
    for r in range(1, rings+1):
        dist = radius_m * (r / rings)
        for k in range(per_ring):
            brg = (360.0 * k) / per_ring
            out.append(offset_latlng(center_lat, center_lng, dist, brg))
    return out

@app.route("/api/eta-centroid", methods=["POST"])
def eta_centroid():
    body = request.get_json(silent=True) or {}
    room_code = (body.get("roomCode") or "").upper()
    radius = int(body.get("searchRadius") or 2000)
    topN = max(1, int(body.get("includeTopN") or 5))
    two_stage = bool(body.get("twoStage") if body.get("twoStage") is not None else True)

    participants = []
    meta = {}
    if room_code and room_code in ROOMS:
        room = ROOMS[room_code]
        meta = room.get("meta") or {}
        for p in room["participants"].values():
            try:
                lat = float(p["lat"]); lng = float(p["lng"])
                if not math.isfinite(lat) or not math.isfinite(lng): continue
                participants.append({"lat":lat, "lng":lng, "mode":p.get("mode","car"),
                                     "pid":p.get("pid"), "nickname":p.get("nickname")})
            except Exception:
                pass
    else:
        for p in body.get("participants") or []:
            try:
                lat = float(p["lat"]); lng = float(p["lng"])
                participants.append({"lat":lat, "lng":lng, "mode":(p.get("mode") or "car")})
            except Exception:
                pass

    if not participants:
        return jsonify({"ok": False, "error": "no_points"}), 400

    seed = time_weighted_centroid(participants) or {"lat":participants[0]["lat"], "lng":participants[0]["lng"]}
    depart_dt = _parse_meeting_time(meta.get("meetingTime"))
    depart_unix = int(depart_dt.replace(tzinfo=timezone.utc).timestamp())

    # 1단계: 거친 탐색
    cand1 = _gen_candidates(seed["lat"], seed["lng"], radius_m=radius, rings=3, per_ring=16)
    scores1 = []
    for (clat, clng) in cand1:
        etas = _etas_for_destination(participants, clat, clng, depart_unix)
        total = sum(etas); mx = max(etas); avg = total / max(len(etas),1)
        scores1.append({"lat":clat, "lng":clng, "etas":etas, "sum":total, "max":mx, "avg":avg})
    scores1.sort(key=lambda x: (x["max"], x["sum"], x["avg"]))
    top = scores1[:topN]

    # 2단계: 상위 후보 주변 미세 탐색
    cand2 = []
    scores2 = []
    if two_stage and top:
        for t in top:
            cand2.extend(_gen_candidates(t["lat"], t["lng"], radius_m=max(200, radius//4), rings=2, per_ring=12))
        seen = set(); uniq = []
        for a,b in cand2:
            k = (round(a,6), round(b,6))
            if k in seen: continue
            seen.add(k); uniq.append((a,b))
        cand2 = uniq
        for (clat, clng) in cand2:
            etas = _etas_for_destination(participants, clat, clng, depart_unix)
            total = sum(etas); mx = max(etas); avg = total / max(len(etas),1)
            scores2.append({"lat":clat, "lng":clng, "etas":etas, "sum":total, "max":mx, "avg":avg})
        scores2.sort(key=lambda x: (x["max"], x["sum"], x["avg"]))
        best = scores2[0] if scores2 else top[0]
        stage2_count = len(cand2)
    else:
        best = top[0]; stage2_count = 0

    # 참가자별 ETA 리포트
    participants_eta = []
    for i, p in enumerate(participants):
        participants_eta.append({
            "index": i,
            "pid": p.get("pid"),
            "nickname": p.get("nickname"),
            "mode": p.get("mode","car"),
            "eta_min": best["etas"][i] if i < len(best["etas"]) else None
        })

    payload = {
        "ok": True,
        "seed": {"lat": seed["lat"], "lng": seed["lng"]},
        "best": {"lat": best["lat"], "lng": best["lng"]},
        "candidate_count_stage1": len(cand1),
        "candidate_count_stage2": stage2_count,
        "participants_eta": participants_eta,
        "ranking": "max_then_sum"
    }

    if room_code in ROOMS:
        ROOMS[room_code]["eta"] = payload
        ROOMS[room_code]["ver"] += 1
        _cleanup_and_save()

    return jsonify(payload)

# ── Suggest
@app.route("/api/meeting-suggest", methods=["POST"])
def meeting_suggest():
    payload = request.get_json(silent=True) or {}
    room_code = (payload.get("roomCode") or "").upper()
    category = payload.get("category") or "FD6"
    radius = int(payload.get("radius") or 2000)
    query = (payload.get("query") or "").strip()

    pts = []
    if room_code and room_code in ROOMS:
        for p in ROOMS[room_code]["participants"].values():
            if isinstance(p.get("lat"),(int,float)) and isinstance(p.get("lng"),(int,float)):
                pts.append({"lat":p["lat"],"lng":p["lng"],"mode":p["mode"]})
    else:
        for p in payload.get("participants") or []:
            try: pts.append({"lat":float(p["lat"]), "lng":float(p["lng"]), "mode":(p.get("mode") or "car")})
            except: pass

    if not pts: return jsonify({"ok":False,"error":"no_points"})

    centroid = time_weighted_centroid(pts)

    # Kakao 검색
    if category in ("BAR","PUB"):
        tokens = query.split() if query else ["술집","호프","바","이자카야","와인바","pub","bar","펍","칵테일바"]
        pool = {}; last_err = None
        for t in tokens:
            res = kakao_keyword_search(centroid["lat"], centroid["lng"], t, radius, category_group_code="FD6")
            if res.get("ok"):
                for d in res["items"]: pool[d["id"]] = d
            else: last_err = res
        if not pool and last_err: return jsonify(last_err), 502
        items = list(pool.values())
    elif query:
        res = kakao_keyword_search(centroid["lat"], centroid["lng"], query, radius,
                                   category_group_code=(category if category in ("FD6","CE7","AD5") else None))
        if not res.get("ok"): return jsonify(res), 502
        items = res["items"]
    else:
        res = kakao_category_search(centroid["lat"], centroid["lng"], category, radius)
        if not res.get("ok"): return jsonify(res), 502
        items = res["items"]

    # Google 보강(상위 12개만)
    if GOOGLE_API_KEY:
        for d in items[:12]:
            try:
                lat, lng = float(d["y"]), float(d["x"])
                d.update({k:v for k,v in google_enrich(d["place_name"], lat, lng, category).items() if v is not None})
            except Exception:
                pass

    # meetingTime 기준으로 영업시간 필터
    meeting_dt = _parse_meeting_time(ROOMS[room_code]["meta"].get("meetingTime")) if room_code in ROOMS else datetime.now()
    req_minutes = 120 if category in ("BAR","PUB") else 60

    filtered = []
    for d in items:
        try:
            d["_centroid_dist_km"] = round(haversine_km(centroid["lat"], centroid["lng"], float(d["y"]), float(d["x"])), 3)
        except:
            d["_centroid_dist_km"] = None

        periods = d.get("_periods")
        if periods:
            rs = _minutes_open_after(meeting_dt, periods)
            if rs is None: continue
            left_min, close_dt = rs
            d["_open_minutes_left"] = left_min
            d["_closes_at"] = close_dt.strftime("%H:%M")
            d["_open_enough"] = (left_min >= req_minutes)
            if not d["_open_enough"]: continue
        else:
            d["_open_enough"] = None

        filtered.append(d)

    def _rank_key(x):
        rank = 0 if x.get("_open_enough") is True else 1
        return (rank, x.get("_centroid_dist_km") is None, x.get("_centroid_dist_km") or 0.0)

    filtered.sort(key=_rank_key)

    result_payload = {"ok": True, "count": len(filtered), "centroid": centroid, "items": filtered}

    if room_code in ROOMS:
        ROOMS[room_code]["results"] = {"count": len(filtered), "centroid": centroid, "items": filtered}
        ROOMS[room_code]["ver"] += 1
        _cleanup_and_save()

    return jsonify(result_payload)

# ─────────────────────────────────────────────────────────────────────────────
# 정적 서빙 (반드시 API 라우트들 아래)
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    resp = make_response(send_from_directory(app.static_folder, "index.html"))
    resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp

@app.route("/<path:fname>")
def static_files(fname):
    resp = make_response(send_from_directory(app.static_folder, fname))
    if fname.endswith((".js", ".css", ".html")):
        resp.headers["Cache-Control"] = "no-store, max-age=0"
    return resp

# ── main
if __name__ == "__main__":
    log.info("Serving static from: %s", STATIC_DIR)
    log.info("KAKAO_REST_KEY=%s, GOOGLE_API_KEY=%s", bool(KAKAO_REST_KEY), bool(GOOGLE_API_KEY))
    app.run(host="0.0.0.0", port=5000, debug=False)
