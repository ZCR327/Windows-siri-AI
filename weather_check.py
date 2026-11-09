# -*- coding: utf-8 -*-
"""Weather module self-check (offline by default, --live hits the network).

Checks:
  A. the place name is split into candidates we can geocode (district first)
  B. the WMO code table (Chinese / English / icon)
  C. the payload shape the panel needs
  D. --live: a real lookup

Output is ASCII on purpose: the Windows console here is GBK.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import weather

FAILS = []


def check(name, got, expect):
    ok = (got == expect)
    if not ok:
        FAILS.append("%s: expect=%r got=%r" % (name, expect, got))
    print("  [%s] %-54s expect=%-22r got=%r"
          % ("OK " if ok else "FAIL", name, expect, got))
    return ok


def part_a():
    print("=== A. place name -> geocode candidates ===")
    c = weather.place_candidates("上海市浦东新区（川沙新镇）")
    check("district first", c[0] if c else None, "浦东新区")
    check("city is in the list", "上海市" in c, True)
    check("full name kept", "上海市浦东新区" in c, True)
    check("district short form", "浦东" in c, True)
    c2 = weather.place_candidates("北京市")
    check("a bare city", c2[0] if c2 else None, "北京市")
    c3 = weather.place_candidates("杭州市西湖区")
    check("district of another city", c3[0] if c3 else None, "西湖区")
    check("empty input", weather.place_candidates(""), [])
    check("day_index(明天)", weather.day_index("明天"), 1)
    check("day_index(后天)", weather.day_index("后天"), 2)
    check("day_index(tomorrow)", weather.day_index("tomorrow"), 1)
    check("day_index(空)", weather.day_index(""), 0)
    check("day_index(乱说)", weather.day_index("随便"), 0)


def part_b():
    print("=== B. WMO code table ===")
    check("code 0", weather.describe(0), ("晴", "sun"))
    check("code 2", weather.describe(2), ("多云", "cloud_sun"))
    check("code 3", weather.describe(3), ("阴", "cloud"))
    check("code 61", weather.describe(61), ("小雨", "rain"))
    check("code 65", weather.describe(65), ("大雨", "rain"))
    check("code 75", weather.describe(75), ("大雪", "snow"))
    check("code 95", weather.describe(95), ("雷阵雨", "thunder"))
    check("code 45", weather.describe(45), ("有雾", "fog"))
    check("unknown code", weather.describe(999), ("未知", "cloud"))
    en, _ = weather.describe(61, "en")
    check("english wording", en, "Light rain")
    icons = set(v[2] for v in weather.WMO.values())
    check("icons are from the known set",
          icons <= set(["sun", "cloud", "cloud_sun", "rain", "snow", "fog", "thunder"]),
          True)


def part_c():
    print("=== C. payload shape ===")
    p = {"place": "上海市浦东新区（川沙新镇）", "city": "上海市浦东新区",
         "temp": 16.4, "feels": 16.8, "hi": 18.4, "lo": 14.3, "code": 61,
         "icon": "rain", "text": "小雨", "text_en": "Light rain", "pop": 100,
         "updated": "20:00", "day": "today"}
    s = weather.summary(p)
    check("summary mentions the city", "上海市浦东新区" in s, True)
    check("summary has the high", "18" in s, True)
    check("summary has the low", "14" in s, True)
    for k in ("city", "hi", "lo", "icon", "text", "updated"):
        check("payload has %s" % k, k in p, True)
    en = weather.summary(p, "en")
    check("english summary mentions the city", "上海市浦东新区" in en, True)


def part_d():
    print("=== D. live lookup ===")
    p = weather.fetch("上海市浦东新区（川沙新镇）")
    if not p:
        check("live fetch", weather.last_error() or "no payload", "a payload")
        return
    check("live city", bool(p.get("city")), True)
    check("live hi is a number", isinstance(p.get("hi"), (int, float)), True)
    check("live lo is a number", isinstance(p.get("lo"), (int, float)), True)
    check("live icon is known", p.get("icon") in
          ("sun", "cloud", "cloud_sun", "rain", "snow", "fog", "thunder"), True)
    check("live text is not empty", bool(p.get("text")), True)
    print("   ->", weather.summary(p))


def main():
    part_a()
    part_b()
    part_c()
    if "--live" in sys.argv:
        part_d()
    print("")
    if FAILS:
        print("%d FAIL" % len(FAILS))
        for f in FAILS:
            print("  -", f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
