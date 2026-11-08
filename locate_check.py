# -*- coding: utf-8 -*-
"""位置功能自检。

    python tools\siri_orb\locate_check.py          # 离线判据（不联网）
    python tools\siri_orb\locate_check.py --live   # 真的定位一次

⚠️ 离线部分**不联网**：用的是当年实测抓下来的真实响应样本，
   改了解析逻辑就会在这里露馅（而不是等真到位置上才出问题）。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import locate as L   # noqa: E402

_nfail = 0
_nok = 0


def ok(cond, name, extra=""):
    global _nfail, _nok
    if cond:
        _nok += 1
        print("  PASS  %s" % name)
    else:
        _nfail += 1
        print("  FAIL  %s  %s" % (name, extra))


# ---- 当年实测抓下来的真实响应样本 -------------------------------
BIGDATA = {
    "locality": "浦东新区",
    "city": "上海市",
    "principalSubdivision": "上海市",
    "localityInfo": {"administrative": [
        {"adminLevel": 2, "name": "中华人民共和国"},
        {"adminLevel": 4, "name": "上海市"},
        {"adminLevel": 6, "name": "浦东新区"},
        {"adminLevel": 8, "name": "川沙新镇"},
    ]},
}

PHOTON = {"features": [{"properties": {
    "country": "中国", "state": "上海市", "city": "浦东新区",
    "district": "六团", "name": "川大路", "postcode": "201205"}}]}


def main():
    print("=== locate self-test ===")

    # ---------------- 解析
    bd = L._parse_bigdata(BIGDATA)
    ok(bd is not None, "bigdata payload parsed")
    ok(bd and bd["district"] == "浦东新区", "bigdata -> 区", "-> %r" % (bd or {}).get("district"))
    ok(bd and bd["township"] == "川沙新镇", "bigdata -> 镇", "-> %r" % (bd or {}).get("township"))
    ok(bd and bd["province"] == "上海市", "bigdata -> 省市", "-> %r" % (bd or {}).get("province"))

    ph = L._parse_photon(PHOTON)
    ok(ph is not None, "photon payload parsed")
    ok(ph and ph["district"] == "浦东新区", "photon -> 区", "-> %r" % (ph or {}).get("district"))
    ok(ph and ph["township"] == "六团", "photon -> 街道/镇", "-> %r" % (ph or {}).get("township"))

    ok(L._parse_bigdata({}) is None, "空响应不炸")
    ok(L._parse_photon({"features": []}) is None, "空 features 不炸")

    # ---------------- 拼名字
    t1 = L._format({"province": "上海市", "city": "上海市",
                    "district": "浦东新区", "township": ""})
    ok(t1 == "上海市浦东新区", "省市同名不重复", "-> %r" % t1)
    t2 = L._format({"province": "上海市", "city": "上海市",
                    "district": "浦东新区", "township": "川沙新镇"})
    ok(t2 == "上海市浦东新区（川沙新镇）", "带镇", "-> %r" % t2)
    t3 = L._format({"province": "北京市", "city": "", "district": "朝阳区",
                    "township": ""})
    ok(t3 == "北京市朝阳区", "缺市级也能拼", "-> %r" % t3)

    # ---------------- 开关
    ok(L.enabled({"ctx": {"location": True}}), "默认开启")
    ok(not L.enabled({"ctx": {"location": False}}), "关掉就是关掉")
    ok(not L.text({"ctx": {"location": False}}), "关掉后不返回位置")

    # ---------------- 不阻塞 + 隐私
    t = time.time()
    v = L.text({"ctx": {"location": True}})
    dt = time.time() - t
    ok(dt < 0.5, "text() 不联网、不阻塞", "-> %.3fs" % dt)
    d = L.describe()
    blob = repr(d)
    ok("lat" not in blob and "lon" not in blob,
       "describe() 里没有经纬度", "-> %s" % blob[:120])

    print("%d checks, %d FAIL" % (_nok + _nfail, _nfail))

    if "--live" in sys.argv:
        print("=== live locate ===")
        t0 = time.time()
        g = L.gps(force=True)
        if not g:
            print("  WARN  no GPS position (location service off?)")
        else:
            print("  GPS ok  accuracy=%s m  in %.1fs" % (g.get("acc"), time.time() - t0))
            p = L.reverse(g["lat"], g["lon"])
            if p:
                print("  place  %s  (source=%s)" % (p.get("text"), p.get("source")))
                ok(bool(p.get("text")), "live: 拿到区级文字")
            else:
                ok(False, "live: 逆地理编码失败")
    return 1 if _nfail else 0


if __name__ == "__main__":
    sys.exit(main())
