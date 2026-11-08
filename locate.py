# -*- coding: utf-8 -*-
"""大致位置：Windows 定位 -> 逆地理编码 -> **区级**文字。

三条通道，自动降级（都是 2026-11-08 在本机实测过的）：

  1) GPS —— Windows 自带的 Geolocator（WinRT），经纬度，实测精度 102m。
  2) 逆地理编码 —— 把经纬度翻成地名：
       - 高德 /v3/geocode/regeo：密钥库里装了 amap key 才用，国内最准；
       - bigdatacloud：免 key，国内可达，要 localityLanguage=zh-Hans 才是简体；
       - Photon / OpenStreetMap：免 key，国内可达，简体，给到街道/镇。
  3) 兜底 —— ip-api 的 IP 定位：**会飘几十公里**，只能当「大概在哪座城」。

隐私（写死的规矩）：
  - 只有**区级文字**会进对话上下文；
  - **经纬度不写日志、不发给模型**，只在内存里活着用来换地名；
  - 开关在设置面板「上下文 -> 附带大致位置」，关掉就完全不查。

为什么不用系统自带的 Geolocator 直接给地名：它只给经纬度，
「XX 区」这种中文行政区划要靠逆地理编码才有。
"""
import json
import os
import subprocess
import threading
import time

try:
    import requests
except Exception:      # pragma: no cover
    requests = None

try:
    import paths as pathmod
except Exception:      # pragma: no cover
    pathmod = None

try:
    import keys as keymod
except Exception:      # pragma: no cover
    keymod = None

HERE = os.path.dirname(os.path.abspath(__file__))

# GPS 缓存多久（秒）—— 定位本身要 1~4 秒，别每次都重来
GPS_TTL = 300
# 地名缓存多久（秒）—— 走配置里的 ctx.location_ttl_min
PLACE_TTL_DEFAULT = 30 * 60
# IP 兜底缓存（秒）—— IP 定位本来就飘，别老查
IP_TTL = 30 * 60

_lock = threading.Lock()
_state = {"gps": None, "place": None, "last_err": ""}
_cfg_provider = None
_warm_thread = None


def _script_dir():
    """locate.ps1 在哪：打包后在解包目录（只读），源码在脚本目录。"""
    if pathmod is not None:
        try:
            return pathmod.bundle_dir()
        except Exception:
            pass
    return HERE


PS_SCRIPT = os.path.join(_script_dir(), "locate.ps1")


# ------------------------------------------------------------------ 配置
def set_cfg_provider(fn):
    """常驻助手把自己那份活配置塞进来（热重载后会变）。"""
    global _cfg_provider
    _cfg_provider = fn


def _cfg():
    if _cfg_provider is not None:
        try:
            return _cfg_provider() or {}
        except Exception:
            return {}
    try:
        import cfgstore
        return cfgstore.load()
    except Exception:
        return {}


def enabled(cfg=None):
    """要不要用位置（设置面板里的开关）。"""
    c = cfg if cfg is not None else _cfg()
    return bool(((c or {}).get("ctx") or {}).get("location", True))


def ttl_sec(cfg=None):
    c = cfg if cfg is not None else _cfg()
    try:
        n = int(((c or {}).get("ctx") or {}).get("location_ttl_min") or 30)
    except Exception:
        n = 30
    return max(1, min(240, n)) * 60


# ------------------------------------------------------------------ GPS
def gps(force=False, timeout=25):
    """拿经纬度。拿不到返回 None（不会抛）。"""
    with _lock:
        g = _state.get("gps")
        if g and not force and (time.time() - g.get("ts", 0)) < GPS_TTL:
            return g
    if not os.path.exists(PS_SCRIPT):
        _state["last_err"] = "locate.ps1 not found"
        return None
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", PS_SCRIPT, "-TimeoutMs", "12000"],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        _state["last_err"] = "ps failed: %r" % (e,)
        return None
    lat = lon = acc = None
    err = ""
    for line in (p.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("LAT="):
            lat = _f(line[4:])
        elif line.startswith("LON="):
            lon = _f(line[4:])
        elif line.startswith("ACC="):
            acc = _f(line[4:])
        elif line.startswith("ERR="):
            err = line[4:]
    if lat is None or lon is None:
        _state["last_err"] = err or "no position"
        return None
    out = {"lat": lat, "lon": lon, "acc": acc, "ts": time.time()}
    with _lock:
        _state["gps"] = out
    return out


def _f(s):
    try:
        return float(str(s).strip())
    except Exception:
        return None


# ---------------------------------------------------------- 逆地理编码
def _amap_key():
    if keymod is None:
        return ""
    try:
        return keymod.get("amap")
    except Exception:
        return ""


def _adm_by_level(adm, level):
    for a in adm or []:
        try:
            if int(a.get("adminLevel")) == level:
                return a.get("name") or ""
        except Exception:
            continue
    return ""


def _reverse_amap(lat, lon):
    key = _amap_key()
    if not key or requests is None:
        return None
    try:
        r = requests.get(
            "https://restapi.amap.com/v3/geocode/regeo",
            params={"key": key, "location": "%.6f,%.6f" % (lon, lat),
                    "extensions": "base"},
            timeout=12)
        d = r.json()
    except Exception:
        return None
    if d.get("status") != "1":
        return None
    c = (d.get("regeocode") or {}).get("addressComponent") or {}
    district = c.get("district") or ""
    if not district:
        return None
    return {"province": c.get("province") or "", "city": c.get("city") or "",
            "district": district, "township": c.get("township") or "",
            "source": "amap"}


def _parse_bigdata(d):
    """bigdatacloud 的响应 -> 层级（**纯解析，不联网**，自检要用）。"""
    if not isinstance(d, dict) or not d.get("locality"):
        return None
    adm = ((d.get("localityInfo") or {}).get("administrative")) or []
    return {"province": _adm_by_level(adm, 4) or d.get("principalSubdivision") or "",
            "city": d.get("city") or "",
            "district": d.get("locality") or "",
            "township": _adm_by_level(adm, 8),
            "source": "bigdata"}


def _reverse_bigdata(lat, lon):
    if requests is None:
        return None
    try:
        r = requests.get(
            "https://api.bigdatacloud.net/data/reverse-geocode-client",
            params={"latitude": lat, "longitude": lon,
                    "localityLanguage": "zh-Hans"},
            timeout=12)
        d = r.json()
    except Exception:
        return None
    return _parse_bigdata(d)


def _parse_photon(j):
    """Photon / OSM 的响应 -> 层级（**纯解析，不联网**）。

    Photon 对中国的层级：state=省市, city=市（直辖市时其实是区）,
    district=街道/镇 —— 所以区要取 city，镇要取 district。
    """
    if not isinstance(j, dict):
        return None
    feats = j.get("features") or []
    if not feats:
        return None
    p = feats[0].get("properties") or {}
    district = p.get("city") or p.get("district") or ""
    if not district:
        return None
    return {"province": p.get("state") or "", "city": "",
            "district": district,
            "township": (p.get("district") or "") if (p.get("city") or "") else "",
            "source": "photon"}


def _reverse_photon(lat, lon):
    if requests is None:
        return None
    try:
        r = requests.get(
            "https://photon.komoot.io/reverse",
            params={"lat": lat, "lon": lon},
            headers={"User-Agent": "Siri-OS/1.0 (local desktop assistant)"},
            timeout=12)
        j = r.json()
    except Exception:
        return None
    return _parse_photon(j)


def reverse(lat, lon):
    """经纬度 -> 区级地名（自动挑后端）。拿不到返回 None。"""
    for fn in (_reverse_amap, _reverse_bigdata, _reverse_photon):
        try:
            out = fn(lat, lon)
        except Exception:
            out = None
        if out and (out.get("district") or out.get("city")):
            out["text"] = _format(out)
            return out
    return None


def _ip_place():
    """IP 定位兜底 —— 会飘几十公里，只能当「大概在哪座城」。"""
    if requests is None:
        return None
    try:
        r = requests.get("http://ip-api.com/json/", timeout=10)
        d = r.json()
    except Exception:
        return None
    if d.get("status") != "success":
        return None
    out = {"province": d.get("regionName") or "", "city": "",
           "district": d.get("city") or "", "township": "",
           "source": "ip"}
    if not out["district"] and not out["province"]:
        return None
    out["text"] = _format(out)
    return out


def _format(p):
    """把层级拼成一句中文：上海市浦东新区（川沙新镇）。"""
    parts = []
    for k in ("province", "city", "district"):
        v = (p.get(k) or "").strip()
        if v and v not in parts:
            parts.append(v)
    main = "".join(parts)
    town = (p.get("township") or "").strip()
    if town and town not in main:
        main = "%s（%s）" % (main, town)
    return main


# ------------------------------------------------------------------ 对外
def refresh(cfg=None, force=False):
    """重新定位（会阻塞，几秒）。返回地名 dict 或 None。"""
    c = cfg if cfg is not None else _cfg()
    if not enabled(c):
        return None
    ttl = ttl_sec(c)
    with _lock:
        p = _state.get("place")
        if p and not force and (time.time() - p.get("ts", 0)) < ttl:
            return p
    place = None
    g = gps(force=force)
    if g:
        place = reverse(g["lat"], g["lon"])
    if place is None:
        with _lock:
            ip = _state.get("ip")
        if ip and (time.time() - ip.get("ts", 0)) < IP_TTL:
            place = ip
        else:
            ip = _ip_place()
            if ip:
                with _lock:
                    _state["ip"] = ip
                place = ip
    if place:
        place["ts"] = time.time()
        if g:
            place["acc"] = g.get("acc")
        with _lock:
            _state["place"] = place
    return place


def current():
    """**不阻塞**地取缓存里的地名（没有就返回 None）。"""
    with _lock:
        return _state.get("place")


def text(cfg=None):
    """给对话上下文用的一句区级位置（缓存里没有就返回空串）。"""
    if not enabled(cfg):
        return ""
    p = current()
    if not p:
        return ""
    return p.get("text") or ""


def describe():
    """给 doctor / 自检用：当前状态（**不含经纬度**）。"""
    p = current()
    g = _state.get("gps")
    out = {"text": (p or {}).get("text", ""),
           "source": (p or {}).get("source", ""),
           "age_sec": int(time.time() - (p or {}).get("ts", 0)) if p else -1,
           "gps_ok": bool(g),
           "gps_acc_m": (g or {}).get("acc"),
           "last_err": _state.get("last_err", "")}
    return out


def start_warmup(interval_sec=None):
    """后台常驻刷新位置 —— 绝不阻塞对话。

    常驻助手启动时调一次；之后每到 TTL 就自己更新，
    所以「我现在在哪个区」这类问题不用等定位。
    """
    global _warm_thread
    with _lock:
        if _warm_thread is not None and _warm_thread.is_alive():
            return False
        _warm_thread = threading.Thread(target=_warm_loop, args=(interval_sec,),
                                        name="locate-warmup", daemon=True)
    _warm_thread.start()
    return True


def _warm_loop(interval_sec):
    while True:
        try:
            cfg = _cfg()
            if enabled(cfg):
                p = refresh(cfg, force=True)
                if p:
                    # 只记**区级文字**；经纬度永远不进日志
                    print("[locate] location updated: %s (source=%s)" % (
                        p.get("text"), p.get("source")))
        except Exception:
            pass
        wait = interval_sec or ttl_sec() // 4 or 60
        time.sleep(max(60, int(wait)))
