# -*- coding: utf-8 -*-
"""天气：地名 -> 坐标 -> 当天预报，出一份「Live Activity 式」的展示数据。

数据源：Open-Meteo（免费、**免 key**、境内可达）
  地理编码  https://geocoding-api.open-meteo.com/v1/search?name=浦东新区
  预报      https://api.open-meteo.com/v1/forecast?latitude=..&daily=..

⚠️ **绝不发用户的经纬度**：定位模块给的只是区级文字，
   这里拿文字去地理编码，用**地名对应的城区中心坐标**查天气 ——
   查的是「浦东新区」的天气，不是他家楼下那一米。

⚠️ 天气码是 WMO 4677 那一套（Open-Meteo 直接给这个码）。

用法：
    import weather
    p = weather.fetch()             # 查用户所在区
    p = weather.fetch("北京")        # 查指定城市
"""
import json
import re
import time

try:
    from netdeps import requests
except Exception:  # pragma: no cover
    requests = None

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TTL = 900                 # 缓存 15 分钟（天气不会一分钟一变）
TIMEOUT = 12

# WMO 天气码 -> (中文, English, 图标)
WMO = {
    0:  ("晴", "Clear", "sun"),
    1:  ("大部晴朗", "Mostly clear", "sun"),
    2:  ("多云", "Partly cloudy", "cloud_sun"),
    3:  ("阴", "Overcast", "cloud"),
    45: ("有雾", "Fog", "fog"),
    48: ("雾凇", "Rime fog", "fog"),
    51: ("小毛毛雨", "Light drizzle", "rain"),
    53: ("毛毛雨", "Drizzle", "rain"),
    55: ("大毛毛雨", "Heavy drizzle", "rain"),
    56: ("冻毛毛雨", "Freezing drizzle", "rain"),
    57: ("强冻毛毛雨", "Heavy freezing drizzle", "rain"),
    61: ("小雨", "Light rain", "rain"),
    63: ("中雨", "Rain", "rain"),
    65: ("大雨", "Heavy rain", "rain"),
    66: ("冻雨", "Freezing rain", "rain"),
    67: ("强冻雨", "Heavy freezing rain", "rain"),
    71: ("小雪", "Light snow", "snow"),
    73: ("中雪", "Snow", "snow"),
    75: ("大雪", "Heavy snow", "snow"),
    77: ("雪粒", "Snow grains", "snow"),
    80: ("阵雨", "Showers", "rain"),
    81: ("强阵雨", "Heavy showers", "rain"),
    82: ("暴雨", "Violent showers", "rain"),
    85: ("阵雪", "Snow showers", "snow"),
    86: ("强阵雪", "Heavy snow showers", "snow"),
    95: ("雷阵雨", "Thunderstorm", "thunder"),
    96: ("雷阵雨伴冰雹", "Thunderstorm with hail", "thunder"),
    99: ("强雷阵雨伴冰雹", "Severe thunderstorm with hail", "thunder"),
}

_CACHE = {}          # key -> (payload, 时间戳)
_GEO = {}            # 地名 -> (lat, lon, 显示名) 或 None
_LAST_ERR = ""


def last_error():
    return _LAST_ERR


def describe(code, lang="zh"):
    """天气码 -> (文案, 图标类型)。"""
    zh, en, icon = WMO.get(int(code), ("未知", "Unknown", "cloud"))
    return (zh if lang != "en" else en), icon


# ------------------------------------------------------------------ 地名处理
def place_candidates(text):
    """把定位模块给的区级文字拆成几个「可以拿去地理编码」的名字。

    「上海市浦东新区（川沙新镇）」 ->
        ['浦东新区', '上海市', '上海市浦东新区']
    先试区（最准），再试市，最后试整串。
    """
    t = (text or "").strip()
    t = re.split(r"[（(]", t)[0].strip()
    if not t:
        return []
    out = []
    m = re.match(r"^(.{2,8}?[市州盟])(.+)$", t)
    if m:
        city, rest = m.group(1), m.group(2)
        rest = rest.strip()
        if rest:
            out.append(rest)               # 浦东新区
            # 「浦东新区」去掉「新区/区/县」也试一次
            short = re.sub(r"(新区|区|县|旗)$", "", rest)
            if short and short != rest:
                out.append(short)          # 浦东
        out.append(city)                   # 上海市
    else:
        out.append(t)
    out.append(t)
    seen = set()
    res = []
    for x in out:
        x = x.strip()
        if x and x not in seen:
            seen.add(x)
            res.append(x)
    return res


# ------------------------------------------------------------------ 网络
def _get_json(url, params):
    if not requests:
        raise RuntimeError("requests 不可用")
    r = requests.get(url, params=params, timeout=TIMEOUT,
                     headers={"User-Agent": "Siri-OS/1.0"})
    if r.status_code != 200:
        raise RuntimeError("HTTP %d" % r.status_code)
    return r.json()


def geocode(name, lang="zh"):
    """地名 -> (lat, lon, 显示名)。缓存命中就不联网。"""
    global _LAST_ERR
    key = (name, lang)
    if key in _GEO:
        return _GEO[key]
    res = None
    t0 = time.time()
    try:
        data = _get_json(GEOCODE_URL, {
            "name": name, "count": 1,
            "language": "zh" if lang != "en" else "en",
            "format": "json"})
        hits = data.get("results") or []
        if hits:
            h = hits[0]
            nm = h.get("name") or name
            admin = h.get("admin1") or ""
            # ⚠️ 判断方向别搞反：名字里已经含了上级行政区就别再拼，
            #    否则会拼出「上海市上海」这种（2026-11-08 实测面板上就这字）。
            show = nm if (not admin or nm in admin) else (admin + nm)
            res = (float(h.get("latitude")), float(h.get("longitude")), show)
    except Exception as e:
        _LAST_ERR = "geocode %s failed: %r" % (name, e)
    # ⚠️ 这行日志不是噪音：打包后 stdout 落在 logs\siri_orb.log。
    #    天气这条链路以前一段日志都没有，出问题只能靠猜
    #    （2026-11-08：面板死活不上屏，排查全靠复现）。
    print("[weather] geocode %s -> %s (%.1fs) %s"
          % (name, res, time.time() - t0, _LAST_ERR or ""))
    _GEO[key] = res
    return res


def _forecast(lat, lon):
    data = _get_json(FORECAST_URL, {
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,weather_code,apparent_temperature",
        "daily": "temperature_2m_max,temperature_2m_min,weather_code,"
                 "precipitation_probability_max",
        "timezone": "auto", "forecast_days": 3})
    cur = data.get("current") or {}
    daily = data.get("daily") or {}
    def first(seq, default=None):
        try:
            return seq[0]
        except Exception:
            return default
    def at(seq, i):
        try:
            return seq[i]
        except Exception:
            return None
    return {
        "temp": cur.get("temperature_2m"),
        "feels": cur.get("apparent_temperature"),
        "hi": at(daily.get("temperature_2m_max"), 0),
        "lo": at(daily.get("temperature_2m_min"), 0),
        "code": cur.get("weather_code"),
        "pop": first(daily.get("precipitation_probability_max")),
        "updated": (cur.get("time") or "")[-5:],
        "days": [
            {"hi": at(daily.get("temperature_2m_max"), i),
             "lo": at(daily.get("temperature_2m_min"), i),
             "code": at(daily.get("weather_code"), i),
             "pop": at(daily.get("precipitation_probability_max"), i)}
            for i in range(3)
        ],
    }


DAY_KEYS = ("today", "tomorrow", "day_after")


def day_index(word):
    """「明天 / tomorrow / 后天」-> 1 / 2；认不出来就是今天。"""
    w = (word or "").strip().lower()
    if not w:
        return 0
    if "后天" in w or "day after" in w or "dayafter" in w:
        return 2
    if "明天" in w or "明日" in w or "tomorrow" in w:
        return 1
    return 0


def fetch(place=None, cfg=None, lang="zh", force=False, day=0):
    """查天气。

    place 省略 = 查用户当前所在区（用区级文字去地理编码）。
    返回 payload 字典；查不到返回 None（原因看 last_error()）。
    """
    global _LAST_ERR
    _LAST_ERR = ""
    if not place:
        try:
            import locate as locate_mod
            place = (locate_mod.current() or {}).get("text") or ""
        except Exception as e:
            _LAST_ERR = "location module unavailable: %r" % (e,)
            place = ""
    place = (place or "").strip()
    key = (place, lang, day)
    if not force and key in _CACHE:
        payload, ts = _CACHE[key]
        if time.time() - ts < TTL:
            return dict(payload)

    spot = None
    tried = []
    for cand in (place_candidates(place) or [place]):
        tried.append(cand)
        spot = geocode(cand, lang)
        if spot:
            break
    if not spot:
        _LAST_ERR = "geocode failed for %s" % (",".join(tried) or "(empty)")
        print("[weather] no spot for %s (%s)" % (place, _LAST_ERR))
        return None
    lat, lon, show = spot
    t1 = time.time()
    try:
        w = _forecast(lat, lon)
    except Exception as e:
        _LAST_ERR = "forecast failed: %r" % (e,)
        print("[weather] forecast failed (%.1fs): %s" % (time.time() - t1, _LAST_ERR))
        return None
    print("[weather] forecast ok %.1fs hi=%s lo=%s" % (
        time.time() - t1, (w.get("days") or [{}])[0].get("hi"),
        (w.get("days") or [{}])[0].get("lo")))
    days = w.get("days") or []
    di = int(day)
    di = max(0, min(di, len(days) - 1)) if days else 0
    d = days[di] if days else {}
    code = d.get("code")
    if di == 0 and code is None:
        code = w.get("code")
    text, icon = describe(code, lang)
    text_en, _ = describe(code, "en")
    payload = {
        "place": place or show,
        "city": show,
        "temp": w.get("temp") if di == 0 else None,
        "feels": w.get("feels") if di == 0 else None,
        "hi": d.get("hi") if days else w.get("hi"),
        "lo": d.get("lo") if days else w.get("lo"),
        "code": code,
        "icon": icon,
        "text": text,
        "text_en": text_en,
        "pop": d.get("pop") if days else w.get("pop"),
        "updated": w.get("updated"),
        "day": DAY_KEYS[di] if di < len(DAY_KEYS) else "today",
    }
    _CACHE[key] = (dict(payload), time.time())
    return payload


DAY_LABEL_ZH = {"today": "今天", "tomorrow": "明天", "day_after": "后天"}
DAY_LABEL_EN = {"today": "today", "tomorrow": "tomorrow", "day_after": "the day after tomorrow"}


def summary(payload, lang="zh"):
    """给模型看的一句话。"""
    if not payload:
        return ""
    city = payload.get("city") or ""
    dk = payload.get("day") or "today"
    when = DAY_LABEL_ZH.get(dk) if lang != "en" else DAY_LABEL_EN.get(dk, dk)
    hi = payload.get("hi")
    lo = payload.get("lo")
    temp = payload.get("temp")
    txt = payload.get("text") if lang != "en" else payload.get("text_en")
    if lang == "en":
        parts = ["%s %s: %s" % (when, city, txt)]
        if hi is not None and lo is not None:
            parts.append("high %sC, low %sC" % (round(hi), round(lo)))
        if temp is not None:
            parts.append("now %sC" % round(temp))
        if payload.get("pop"):
            parts.append("chance of precipitation %s%%" % payload["pop"])
        return "; ".join(parts)
    parts = ["%s%s：%s" % (when, city, txt)]
    if hi is not None and lo is not None:
        parts.append("最高 %d°，最低 %d°" % (round(hi), round(lo)))
    if temp is not None:
        parts.append("现在 %d°" % round(temp))
    if payload.get("pop"):
        parts.append("降水概率 %d%%" % payload["pop"])
    return "，".join(parts) + "。"
