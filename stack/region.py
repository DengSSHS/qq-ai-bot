#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""他说他在哪 —— 地区配置（2026-11-09）

为什么单独一个模块：
  · 天气要按「他实际在的地方」算。写死湘潭是错的（他放假会回家）。
  · 用户要求：让**她主动问**、问到了就更新，而且要实时生效。

设计：
  城市名 → 经纬度优先查内置表（中文地名走 open-meteo 的 geocoding 会猜错，
  实测「湘潭」被判到安徽），查不到才用 geocoding，并且只接受 country_code=CN 的结果。
  存到 memory/region.json，写完即时生效（context_pack 每次都重新读，不走缓存）。

接口：
    load()                 -> dict|None  {name, lat, lon, updated_at, source}
    set_region(name, lat=None, lon=None) -> (ok, 说明)
    describe()             -> 一行中文，给素材包用
"""
import io
import json
import os
import time
import urllib.parse
import urllib.request

BASE = "/opt/dsh"
PATH = os.path.join(BASE, "memory", "region.json")

# 常用城市（中文名 → 经纬度）。宁可多列几十个，也别让 geocoding 瞎猜。
CITIES = {
    "湘潭": (27.83, 112.94), "长沙": (28.23, 112.94), "株洲": (27.83, 113.13),
    "北京": (39.90, 116.41), "上海": (31.23, 121.47), "天津": (39.13, 117.20),
    "重庆": (29.56, 106.55), "广州": (23.13, 113.26), "深圳": (22.54, 114.06),
    "东莞": (23.02, 113.75), "佛山": (23.02, 113.12), "珠海": (22.27, 113.58),
    "杭州": (30.27, 120.15), "宁波": (29.87, 121.55), "温州": (28.00, 120.70),
    "南京": (32.06, 118.80), "苏州": (31.30, 120.62), "无锡": (31.49, 120.31),
    "合肥": (31.82, 117.23), "福州": (26.07, 119.30), "厦门": (24.48, 118.09),
    "南昌": (28.68, 115.86), "武汉": (30.59, 114.31), "郑州": (34.75, 113.63),
    "济南": (36.65, 117.12), "青岛": (36.07, 120.38), "石家庄": (38.04, 114.51),
    "太原": (37.87, 112.55), "西安": (34.34, 108.94), "兰州": (36.06, 103.83),
    "成都": (30.57, 104.07), "绵阳": (31.47, 104.68), "昆明": (25.04, 102.72),
    "贵阳": (26.65, 106.63), "南宁": (22.82, 108.32), "海口": (20.04, 110.32),
    "沈阳": (41.81, 123.43), "大连": (38.91, 121.61), "长春": (43.82, 125.32),
    "哈尔滨": (45.80, 126.53), "呼和浩特": (40.84, 111.75), "银川": (38.49, 106.23),
    "西宁": (36.62, 101.78), "乌鲁木齐": (43.83, 87.62), "拉萨": (29.65, 91.14),
    "香港": (22.32, 114.17), "澳门": (22.20, 113.54), "台北": (25.03, 121.57),
}


def load():
    try:
        d = json.loads(io.open(PATH, encoding="utf-8").read())
        if d.get("lat") is not None and d.get("lon") is not None:
            return d
    except Exception:
        pass
    return None


def save(d):
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    io.open(tmp, "w", encoding="utf-8").write(json.dumps(d, ensure_ascii=False, indent=1))
    os.replace(tmp, PATH)


def _geocode(name):
    url = ("https://geocoding-api.open-meteo.com/v1/search?name=%s&count=5&language=zh&format=json"
           % urllib.parse.quote(name))
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
    with urllib.request.urlopen(req, timeout=12) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    for hit in d.get("results") or []:
        if (hit.get("country_code") or "").upper() == "CN":
            return float(hit["latitude"]), float(hit["longitude"]), \
                   "%s%s" % (hit.get("name") or name, hit.get("admin1") or "")
    return None


def set_region(name, lat=None, lon=None, source="对话"):
    """他说'我在长沙'之后调用。返回 (ok, 说明)。"""
    name = (name or "").strip()
    if not name and lat is None:
        return False, "要给个地名"
    try:
        if lat is None or lon is None:
            key = name.replace("市", "").replace("省", "")
            if key in CITIES:
                lat, lon = CITIES[key]
                name = key
            else:
                hit = _geocode(name)
                if not hit:
                    return False, "查不到「%s」这个地名（只能先用经纬度）" % name
                lat, lon, resolved = hit
                name = resolved
        d = dict(name=name, lat=float(lat), lon=float(lon),
                 updated_at=time.strftime("%F %T"), source=source)
        save(d)
        return True, "记住了：你在 %s（%.2f, %.2f）" % (d["name"], d["lat"], d["lon"])
    except Exception as e:
        return False, "存不进去：%s" % str(e)[:120]


def describe():
    d = load()
    if not d:
        return ""
    return "%s（%.2f, %.2f，%s 记的）" % (d["name"], d["lat"], d["lon"], d.get("updated_at", "")[:10])


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        print(set_region(sys.argv[1]))
    else:
        print(describe() or "还没设置地区")
