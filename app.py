import time
import json
import os
import psycopg
from pathlib import Path

# V9 PUSH PUBLIC KEY 135-6-5
def load_local_env():
    path = Path(".env")
    if not path.exists():
        return

    for line in path.read_text().splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())

load_local_env()
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
import requests

app=FastAPI()
templates=Jinja2Templates(directory="templates")
BASE="https://everse.emart24.co.kr"
WEB="https://emart24.co.kr"
s=requests.Session()
s.headers.update({"User-Agent":"Mozilla/5.0","Accept":"application/json, text/plain, */*","x-requested-with":"XMLHttpRequest"})

@app.on_event("startup")
def startup():
    init_db()

def search_products(q):
    r=s.post(BASE+"/stock/stock/search",
        data={"currentPage":1,"pageCnt":20,"sortType":"SALE","saleProductYn":"N","searchWord":q},
        headers={"Content-Type":"application/x-www-form-urlencoded; charset=UTF-8"},timeout=20)
    r.raise_for_status()
    return [{"plu":str(x.get("pluCd","")),"name":x.get("goodsNm",""),"price":x.get("viewPrice","")}
            for x in r.json().get("productList",[])]

def get_store_codes(a1,a2):
    codes = []
    seen = set()
    page = 1

    while page <= 30:
        r = s.get(
            WEB + "/api1/store",
            params={"page": page, "AREA1": a1, "AREA2": a2},
            timeout=20
        )
        r.raise_for_status()
        body = r.json()
        rows = body.get("data") or []

        for x in rows:
            code = str(x.get("CODE", "")).strip()
            if code and str(x.get("USE_YN", "Y")).upper() == "Y":
                if code not in seen:
                    seen.add(code)
                    codes.append(code)

        total = int(body.get("count") or 0)
        if not rows or len(rows) < 40 or len(codes) >= total:
            break

        page += 1

    return codes

HISTORY_FILE=Path("stock_history.json")
DATABASE_URL=os.getenv("DATABASE_URL")

def init_db():
    if not DATABASE_URL:
        return

    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS stock_history (
                        plu TEXT NOT NULL,
                        biz_no TEXT NOT NULL,
                        store_name TEXT,
                        address TEXT,
                        quantity INTEGER,
                        last_restock TIMESTAMP,
                        increase INTEGER,
                        from_qty INTEGER,
                        to_qty INTEGER,
                        PRIMARY KEY (plu, biz_no)
                    )
                """)
                # V9 PUSH DATABASE 135-6-2
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS push_subscriptions (
                        id BIGSERIAL PRIMARY KEY,
                        endpoint TEXT NOT NULL UNIQUE,
                        p256dh TEXT NOT NULL,
                        auth TEXT NOT NULL,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                """)

                cur.execute("""
                    CREATE TABLE IF NOT EXISTS push_watch_items (
                        id BIGSERIAL PRIMARY KEY,
                        subscription_id BIGINT NOT NULL
                            REFERENCES push_subscriptions(id)
                            ON DELETE CASCADE,
                        plu TEXT NOT NULL,
                        product_name TEXT NOT NULL,
                        radius_km INTEGER NOT NULL DEFAULT 3,
                        latitude DOUBLE PRECISION,
                        longitude DOUBLE PRECISION,
                        enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        last_stock JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMPTZ DEFAULT NOW(),
                        UNIQUE (subscription_id, plu)
                    )
                """)

                # V9 PUSH OUTBOX 135-6-25
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS push_outbox (
                        id BIGSERIAL PRIMARY KEY,
                        watch_id BIGINT NOT NULL
                            REFERENCES push_watch_items(id)
                            ON DELETE CASCADE,
                        biz_no TEXT NOT NULL,
                        store_name TEXT NOT NULL,
                        quantity INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        attempts INTEGER NOT NULL DEFAULT 0,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        sent_at TIMESTAMPTZ,
                        last_error TEXT
                    )
                """)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS
                        idx_push_outbox_pending
                    ON push_outbox (status, created_at)
                """)

            conn.commit()
    except Exception as e:
        print("DB startup unavailable:", e)


def load_history():
    try:
        return json.loads(HISTORY_FILE.read_text())
    except:
        return {"products":{}}

def save_history(data):
    HISTORY_FILE.write_text(json.dumps(data,ensure_ascii=False,indent=2))

def track_stock(plu, stores):
    # 로컬에서는 기존 JSON 방식 사용
    if not DATABASE_URL:
        history=load_history()
        products=history.setdefault("products",{})
        product=products.setdefault(str(plu),{"stores":{}})
        old_stores=product.setdefault("stores",{})
        now=datetime.now().isoformat(timespec="seconds")

        for store in stores:
            if store["stockStatus"]!="known":
                continue

            biz=store["bizNo"]
            current=store["quantity"]
            old=old_stores.get(biz,{})
            previous=old.get("quantity")

            if previous is not None and current>previous:
                old["lastRestock"]=now
                old["increase"]=current-previous
                old["fromQty"]=previous
                old["toQty"]=current

            old["quantity"]=current
            old["name"]=store["name"]
            old["address"]=store["address"]
            old_stores[biz]=old

            store["lastRestock"]=old.get("lastRestock")
            store["lastIncrease"]=old.get("increase")
            store["fromQty"]=old.get("fromQty")
            store["toQty"]=old.get("toQty")

        save_history(history)
        return stores

    # Render에서는 PostgreSQL 사용
    try:
        return track_stock_db(plu, stores)
    except Exception as e:
        print("DB history unavailable:", e)
        return stores

def track_stock_db(plu, stores):
    with psycopg.connect(DATABASE_URL, connect_timeout=3) as conn:
        with conn.cursor() as cur:
            for store in stores:
                if store["stockStatus"]!="known":
                    continue

                biz=str(store["bizNo"])
                current=store["quantity"]

                cur.execute("""
                    SELECT quantity, last_restock, increase, from_qty, to_qty
                    FROM stock_history
                    WHERE plu=%s AND biz_no=%s
                """, (str(plu), biz))

                row=cur.fetchone()

                last_restock=None
                increase=None
                from_qty=None
                to_qty=None

                if row:
                    previous=row[0]
                    last_restock=row[1]
                    increase=row[2]
                    from_qty=row[3]
                    to_qty=row[4]

                    if previous is not None and current>previous:
                        last_restock=datetime.now()
                        increase=current-previous
                        from_qty=previous
                        to_qty=current

                cur.execute("""
                    INSERT INTO stock_history
                    (plu,biz_no,store_name,address,quantity,last_restock,increase,from_qty,to_qty)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (plu,biz_no)
                    DO UPDATE SET
                        store_name=EXCLUDED.store_name,
                        address=EXCLUDED.address,
                        quantity=EXCLUDED.quantity,
                        last_restock=EXCLUDED.last_restock,
                        increase=EXCLUDED.increase,
                        from_qty=EXCLUDED.from_qty,
                        to_qty=EXCLUDED.to_qty
                """, (
                    str(plu), biz, store["name"], store["address"],
                    current, last_restock, increase, from_qty, to_qty
                ))

                store["lastRestock"]=last_restock.isoformat() if last_restock else None
                store["lastIncrease"]=increase
                store["fromQty"]=from_qty
                store["toQty"]=to_qty

        conn.commit()

    return stores


def validate_stock_response(body):
    """Reject upstream sentinel/malformed responses; never treat them as zero stock."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=503, detail="이마트24 재고 서버 응답 형식이 올바르지 않습니다.")
    for field in ("storeInfo", "storeGoodsQty"):
        rows = body.get(field)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise HTTPException(
                status_code=503,
                detail="이마트24 재고 서버가 정상 재고 목록을 반환하지 않았습니다. 잠시 후 다시 시도해 주세요."
            )
    return body

def get_inventory(plu,a1,a2):
    codes=get_store_codes(a1,a2)
    s.post(BASE+"/api/stock/v1/search/keyword",
        data={"pluCd":plu,"searchPage":"STOCK_SEARCH_SERVICE"},
        headers={"Content-Type":"application/x-www-form-urlencoded; charset=UTF-8"},timeout=20)
    info,qty={},{}
    for i in range(0,len(codes),20):
        r=s.get(BASE+"/api/stock/v3/stock-search/store",
            params={"searchPluCode":plu,"bizNoArr":",".join(codes[i:i+20])},timeout=20)
        r.raise_for_status()
        body=validate_stock_response(r.json())
        for x in body.get("storeInfo") or []:
            biz=str(x.get("BIZNO","")).strip()
            if biz: info[biz]={"bizNo":biz,"name":x.get("BIZNM",""),"address":x.get("STOREADDR",""),"lat":x.get("LAT"),"lon":x.get("LON")}
        for x in body.get("storeGoodsQty") or []:
            biz=str(x.get("BIZNO","")).strip()
            if biz: qty[biz]=x.get("BIZQTY")
    out=[]
    for biz,x in info.items():
        y=dict(x)
        if biz in qty:
            try:y["quantity"]=int(float(qty[biz]))
            except:y["quantity"]=0
            y["stockStatus"]="known"
        else:
            y["quantity"]=None;y["stockStatus"]="unknown"
        out.append(y)
    out.sort(key=lambda x:(x["stockStatus"]!="known",-(x["quantity"] or 0),x["name"]))
    return track_stock(plu,out)

@app.get("/",response_class=HTMLResponse)
def home(request:Request): return templates.TemplateResponse(request, "index.html")
product_cache = {}
PRODUCT_CACHE_SECONDS = 60

@app.get("/api/products")
def products(q:str):
    try:
        key = q.strip().lower()
        now = time.time()
        cached = product_cache.get(key)

        if cached and now - cached["time"] < PRODUCT_CACHE_SECONDS:
            return {"products": cached["products"], "cached": True}

        products = search_products(q)
        product_cache[key] = {
            "time": now,
            "products": products
        }

        return {"products": products, "cached": False}

    except Exception as e:
        raise HTTPException(502,str(e))
inventory_cache = {}
CACHE_SECONDS = 10

@app.get("/api/inventory")
def inventory(plu:str,area1="서울특별시",area2="광진구"):
    try:
        key = (plu, area1, area2)
        now = time.time()
        cached = inventory_cache.get(key)

        if cached and now - cached["time"] < CACHE_SECONDS:
            return {"stores": cached["stores"], "cached": True}

        stores = get_inventory(plu, area1, area2)
        inventory_cache[key] = {
            "time": now,
            "stores": stores
        }

        return {"stores": stores, "cached": False}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502,str(e))


@app.get("/api/inventory/nearby")
def inventory_nearby(plu: str, area1: str, areas: str):
    districts = list(dict.fromkeys(
        x.strip() for x in areas.split(",") if x.strip()
    ))
    if not 1 <= len(districts) <= 8:
        raise HTTPException(400, "검색 지역은 1~8개까지 가능합니다.")

    combined = {}
    try:
        for district in districts:
            key = (plu, area1, district)
            now = time.time()
            cached = inventory_cache.get(key)
            if cached and now - cached["time"] < CACHE_SECONDS:
                rows = cached["stores"]
            else:
                rows = get_inventory(plu, area1, district)
                inventory_cache[key] = {
                    "time": time.time(),
                    "stores": rows
                }
            for store in rows:
                combined[str(store["bizNo"])] = store

        return {"stores": list(combined.values()), "areas": districts}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))


@app.get("/api/inventory/region")
def inventory_region(plu: str, regions: str):
    pairs = []
    for item in regions.split(","):
        if ":" not in item:
            raise HTTPException(400, "지역 형식이 올바르지 않습니다.")
        city, district = [x.strip() for x in item.split(":", 1)]
        if not city or not district:
            raise HTTPException(400, "지역명이 비어 있습니다.")
        if (city, district) not in pairs:
            pairs.append((city, district))

    if not 1 <= len(pairs) <= 8:
        raise HTTPException(400, "지역은 1~8개까지 가능합니다.")

    combined = {}
    try:
        for city, district in pairs:
            key = (plu, city, district)
            cached = inventory_cache.get(key)

            if cached and time.time() - cached["time"] < CACHE_SECONDS:
                rows = cached["stores"]
            else:
                rows = get_inventory(plu, city, district)
                inventory_cache[key] = {
                    "time": time.time(),
                    "stores": rows
                }

            for store in rows:
                combined[str(store["bizNo"])] = store

        return {
            "stores": list(combined.values()),
            "regions": [
                {"city": city, "district": district}
                for city, district in pairs
            ]
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))


# 서울·경기 매장 위치 캐시 (30분)
store_location_cache = {"time": 0, "stores": []}
STORE_LOCATION_CACHE_SECONDS = 1800

def get_all_store_locations():
    now = time.time()

    if (store_location_cache["stores"] and
        now - store_location_cache["time"] < STORE_LOCATION_CACHE_SECONDS):
        return store_location_cache["stores"]

    combined = {}

    for city in ["서울특별시", "경기도"]:
        page = 1

        while True:
            r = s.get(
                WEB + "/api1/store",
                params={"page": page, "AREA1": city, "AREA2": ""},
                timeout=20
            )
            r.raise_for_status()
            body = r.json()
            rows = body.get("data") or []

            for x in rows:
                code = str(x.get("CODE", "")).strip()

                if not code:
                    continue

                try:
                    lat = float(x.get("LATITUDE"))
                    lon = float(x.get("LONGITUDE"))
                except (TypeError, ValueError):
                    continue

                if not (33 <= lat <= 39 and 124 <= lon <= 132):
                    continue

                combined[code] = {
                    "bizNo": code,
                    "name": x.get("TITLE", ""),
                    "address": x.get("ADDRESS", ""),
                    "lat": lat,
                    "lon": lon
                }

            total = int(body.get("count") or 0)

            if not rows or page * 40 >= total:
                break

            page += 1

    result = list(combined.values())
    store_location_cache["stores"] = result
    store_location_cache["time"] = time.time()

    return result


def distance_km(lat1, lon1, lat2, lon2):
    from math import radians, sin, cos, asin, sqrt

    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)

    a = (
        sin(dlat / 2) ** 2
        + cos(radians(lat1))
        * cos(radians(lat2))
        * sin(dlon / 2) ** 2
    )

    return 6371 * 2 * asin(sqrt(min(1, a)))


def get_nearby_stores(lat, lon, radius_km):
    if not (33 <= lat <= 39 and 124 <= lon <= 132):
        raise ValueError("GPS 좌표가 올바르지 않습니다.")

    if not (0 < radius_km <= 20):
        raise ValueError("검색 반경은 1~20km 사이여야 합니다.")

    stores = get_all_store_locations()
    nearby = []

    for store in stores:
        distance = distance_km(
            lat, lon, store["lat"], store["lon"]
        )

        if distance <= radius_km:
            item = dict(store)
            item["distanceKm"] = round(distance, 2)
            nearby.append(item)

    nearby.sort(key=lambda x: x["distanceKm"])
    return nearby

def get_inventory_by_gps(plu,lat,lon,radius_km):
    from concurrent.futures import ThreadPoolExecutor

    nearby = get_nearby_stores(lat, lon, radius_km)
    codes = [x["bizNo"] for x in nearby]

    if not codes:
        return track_stock(plu, [])

    s.post(BASE+"/api/stock/v1/search/keyword",
        data={"pluCd":plu,"searchPage":"STOCK_SEARCH_SERVICE"},
        headers={"Content-Type":"application/x-www-form-urlencoded; charset=UTF-8"},
        timeout=20)

    batches = [codes[i:i+20] for i in range(0,len(codes),20)]

    def fetch_batch(batch):
        # 동시 요청 간 Session 상태 공유를 피하기 위해 개별 요청 사용
        r = requests.get(
            BASE+"/api/stock/v3/stock-search/store",
            params={"searchPluCode":plu,"bizNoArr":",".join(batch)},
            headers=s.headers,
            timeout=20
        )
        r.raise_for_status()
        return validate_stock_response(r.json())

    info,qty={},{}

    with ThreadPoolExecutor(max_workers=4) as executor:
        for body in executor.map(fetch_batch, batches):
            for x in body.get("storeInfo") or []:
                biz=str(x.get("BIZNO","")).strip()
                if biz:
                    info[biz]={
                        "bizNo":biz,
                        "name":x.get("BIZNM",""),
                        "address":x.get("STOREADDR",""),
                        "lat":x.get("LAT"),
                        "lon":x.get("LON")
                    }

            for x in body.get("storeGoodsQty") or []:
                biz=str(x.get("BIZNO","")).strip()
                if biz:
                    qty[biz]=x.get("BIZQTY")

    out=[]
    for biz,x in info.items():
        y=dict(x)
        if biz in qty:
            try:
                y["quantity"]=int(float(qty[biz]))
            except:
                y["quantity"]=0
            y["stockStatus"]="known"
        else:
            y["quantity"]=None
            y["stockStatus"]="unknown"
        out.append(y)

    out.sort(key=lambda x:(
        x["stockStatus"]!="known",
        -(x["quantity"] or 0),
        x["name"]
    ))
    return track_stock(plu,out)


gps_inventory_cache = {}
GPS_INVENTORY_CACHE_SECONDS = 10

@app.get("/api/inventory/gps")
def inventory_gps(plu: str, lat: float, lon: float, radius: float = 5):
    if not (33 <= lat <= 39 and 124 <= lon <= 132):
        raise HTTPException(400, "GPS 좌표가 올바르지 않습니다.")

    if not (0 < radius <= 20):
        raise HTTPException(400, "검색 반경은 1~20km여야 합니다.")

    key = (plu, round(lat, 5), round(lon, 5), radius)
    now = time.time()
    cached = gps_inventory_cache.get(key)

    if cached and now - cached["time"] < GPS_INVENTORY_CACHE_SECONDS:
        return {
            "stores": cached["stores"],
            "radiusKm": radius,
            "searchType": "gps",
            "cached": True
        }

    try:
        stores = get_inventory_by_gps(plu, lat, lon, radius)
        gps_inventory_cache[key] = {
            "time": time.time(),
            "stores": stores
        }
        return {
            "stores": stores,
            "radiusKm": radius,
            "searchType": "gps",
            "cached": False
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, str(e))


@app.get("/api/push/public-key")
def push_public_key():
    key = os.getenv("VAPID_PUBLIC_KEY", "")
    if not key:
        raise HTTPException(status_code=503, detail="푸시 알림이 설정되지 않았습니다.")
    return {"publicKey": key}


# V9 SERVICE WORKER 135-6-7
@app.get("/sw.js", include_in_schema=False)
def service_worker():
    path = Path("static/sw.js")

    if not path.is_file():
        raise HTTPException(status_code=404, detail="서비스 워커 파일 없음")

    return FileResponse(
        path,
        media_type="application/javascript",
        headers={
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache"
        }
    )


# V9 PUSH SAVE API 135-6-10
from pydantic import BaseModel, Field
from urllib.parse import urlparse

class PushKeys135(BaseModel):
    p256dh: str = Field(min_length=1, max_length=512)
    auth: str = Field(min_length=1, max_length=512)

class PushSubscription135(BaseModel):
    endpoint: str = Field(min_length=1, max_length=2048)
    keys: PushKeys135

@app.post("/api/push/subscribe")
def save_push_subscription(data: PushSubscription135):
    if not DATABASE_URL:
        raise HTTPException(
            status_code=503,
            detail="PostgreSQL 연결이 필요합니다."
        )

    parsed = urlparse(data.endpoint)

    if parsed.scheme != "https" or not parsed.hostname:
        raise HTTPException(
            status_code=400,
            detail="올바르지 않은 푸시 구독 주소입니다."
        )

    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO push_subscriptions
                        (endpoint, p256dh, auth)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (endpoint)
                    DO UPDATE SET
                        p256dh = EXCLUDED.p256dh,
                        auth = EXCLUDED.auth,
                        updated_at = NOW()
                    RETURNING id
                """, (
                    data.endpoint,
                    data.keys.p256dh,
                    data.keys.auth
                ))

                subscription_id = cur.fetchone()[0]
            conn.commit()

        return {
            "ok": True,
            "subscriptionId": subscription_id
        }

    except Exception as e:
        print("Push subscription save failed:", type(e).__name__)
        raise HTTPException(
            status_code=503,
            detail="푸시 구독 저장에 실패했습니다."
        )


# V9 PUSH WATCH API 135-6-13
from pydantic import BaseModel, Field
from typing import Optional

class PushWatchItem135(BaseModel):
    plu: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=300)

class PushWatchRequest135(BaseModel):
    subscription: PushSubscription135
    products: list[PushWatchItem135] = Field(max_length=30)
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    radius: int = Field(default=3, ge=0, le=20)

@app.post("/api/push/watch")
def save_push_watch(data: PushWatchRequest135):
    if not DATABASE_URL:
        raise HTTPException(
            status_code=503,
            detail="PostgreSQL 연결이 필요합니다."
        )

    if (data.latitude is None) != (data.longitude is None):
        raise HTTPException(
            status_code=400,
            detail="위도와 경도는 함께 입력해야 합니다."
        )

    if data.radius > 0 and data.latitude is None:
        raise HTTPException(
            status_code=400,
            detail="반경 검색에는 위치 정보가 필요합니다."
        )

    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id FROM push_subscriptions
                    WHERE endpoint = %s AND p256dh = %s AND auth = %s
                """, (
                    data.subscription.endpoint,
                    data.subscription.keys.p256dh,
                    data.subscription.keys.auth
                ))

                row = cur.fetchone()
                if not row:
                    raise HTTPException(
                        status_code=403,
                        detail="등록된 푸시 구독을 찾을 수 없습니다."
                    )

                subscription_id = row[0]

                # V9 PUSH WATCH UPSERT 135-6-16
                # 기존 상품은 last_stock을 유지하고 설정만 갱신
                product_codes = list(dict.fromkeys(
                    product.plu for product in data.products
                ))

                cur.execute("""
                    DELETE FROM push_watch_items
                    WHERE subscription_id = %s
                      AND NOT (plu = ANY(%s))
                """, (subscription_id, product_codes))

                for product in data.products:
                    cur.execute("""
                        INSERT INTO push_watch_items
                            (subscription_id, plu, product_name,
                             radius_km, latitude, longitude)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (subscription_id, plu)
                        DO UPDATE SET
                            product_name = EXCLUDED.product_name,
                            radius_km = EXCLUDED.radius_km,
                            latitude = EXCLUDED.latitude,
                            longitude = EXCLUDED.longitude
                    """, (
                        subscription_id,
                        product.plu,
                        product.name,
                        data.radius,
                        data.latitude,
                        data.longitude
                    ))

            conn.commit()

        return {"ok": True, "watchCount": len(data.products)}

    except HTTPException:
        raise
    except Exception as e:
        print("Push watch save failed:", type(e).__name__)
        raise HTTPException(
            status_code=503,
            detail="알림 대상 저장에 실패했습니다."
        )


# V9 RESTOCK DETECTOR 135-6-20
def detect_push_restocks(previous_stock, current_stores):
    """
    이전 재고와 현재 재고를 비교한다.
    반환값: (새로운 재고 기록, 재입고 매장 목록)
    """
    if not isinstance(previous_stock, dict):
        previous_stock = {}

    updated = dict(previous_stock)
    restocked = []

    for store in current_stores:
        if store.get("stockStatus") != "known":
            continue

        biz_no = str(store.get("bizNo", "")).strip()
        if not biz_no:
            continue

        try:
            quantity = int(store.get("quantity"))
        except (TypeError, ValueError):
            continue

        if quantity < 0:
            continue

        old_quantity = updated.get(biz_no)

        if (
            type(old_quantity) is int
            and old_quantity == 0
            and quantity > 0
        ):
            restocked.append({
                "bizNo": biz_no,
                "name": store.get("name", ""),
                "quantity": quantity
            })

        updated[biz_no] = quantity

    return updated, restocked


# V9 PUSH WATCH LOADER 135-6-21
def load_push_watch_items(limit=100):
    """활성화된 푸시 알림 대상과 구독 정보를 조회한다."""
    if not DATABASE_URL:
        return []

    limit = max(1, min(int(limit), 500))

    with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    w.id,
                    w.plu,
                    w.product_name,
                    w.radius_km,
                    w.latitude,
                    w.longitude,
                    w.last_stock,
                    s.endpoint,
                    s.p256dh,
                    s.auth
                FROM push_watch_items AS w
                JOIN push_subscriptions AS s
                    ON s.id = w.subscription_id
                WHERE w.enabled = TRUE
                  AND w.latitude IS NOT NULL
                  AND w.longitude IS NOT NULL
                  AND w.radius_km BETWEEN 1 AND 20
                ORDER BY w.id
                LIMIT %s
            """, (limit,))

            rows = cur.fetchall()

    result = []

    for row in rows:
        result.append({
            "watchId": row[0],
            "plu": row[1],
            "productName": row[2],
            "radius": row[3],
            "latitude": row[4],
            "longitude": row[5],
            "lastStock": row[6] or {},
            "subscription": {
                "endpoint": row[7],
                "keys": {
                    "p256dh": row[8],
                    "auth": row[9]
                }
            }
        })

    return result


# V9 PUSH MONITOR 135-6-22
def run_push_stock_monitor(limit=100):
    """등록된 상품의 재고를 확인하고 재입고를 감지한다."""
    if not DATABASE_URL:
        return {"checked": 0, "restocks": 0, "errors": 0}

    watches = load_push_watch_items(limit)
    checked = 0
    restock_count = 0
    errors = 0

    for watch in watches:
        try:
            stores = get_inventory_by_gps(
                watch["plu"],
                watch["latitude"],
                watch["longitude"],
                watch["radius"]
            )

            updated, restocked = detect_push_restocks(
                watch["lastStock"], stores
            )

            # V9 PUSH OUTBOX CONNECT 135-6-26
            # 알림 대기열 저장과 재고 기록 갱신을 하나의 트랜잭션으로 처리
            with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
                with conn.cursor() as cur:
                    for store in restocked:
                        cur.execute("""
                            INSERT INTO push_outbox
                                (watch_id, biz_no, store_name, quantity)
                            VALUES (%s, %s, %s, %s)
                        """, (
                            watch["watchId"],
                            store["bizNo"],
                            store["name"],
                            store["quantity"]
                        ))

                    cur.execute("""
                        UPDATE push_watch_items
                        SET last_stock = %s::jsonb
                        WHERE id = %s
                          AND last_stock = %s::jsonb
                    """, (
                        json.dumps(updated, ensure_ascii=False),
                        watch["watchId"],
                        json.dumps(watch["lastStock"], ensure_ascii=False)
                    ))

                    if cur.rowcount != 1:
                        raise RuntimeError(
                            "재고 기록이 다른 작업에서 변경되었습니다."
                        )

                conn.commit()

            checked += 1

            for store in restocked:
                restock_count += 1
                print(
                    "[재입고 감지]",
                    watch["productName"],
                    store["name"],
                    str(store["quantity"]) + "개"
                )

        except Exception as e:
            errors += 1
            print(
                "[재고 감시 실패]",
                watch["plu"],
                type(e).__name__
            )

    return {
        "checked": checked,
        "restocks": restock_count,
        "errors": errors
    }


# V9 PUSH SENDER 135-6-23
def send_restock_push(subscription, product_name, store_name, quantity):
    """등록된 휴대폰에 재입고 푸시 알림을 전송한다."""
    import base64
    from pywebpush import webpush

    encoded_key = os.getenv("VAPID_PRIVATE_KEY", "")
    public_key = os.getenv("VAPID_PUBLIC_KEY", "")
    subject = os.getenv("VAPID_SUBJECT", "")

    if not encoded_key or not public_key or not subject:
        raise RuntimeError("VAPID 환경변수가 설정되지 않았습니다.")

    private_key = base64.b64decode(encoded_key).decode("utf-8")

    payload = {
        "title": "🔔 이마트24 재입고 알림",
        "body": f"{product_name} · {store_name} · 재고 {quantity}개",
        "tag": "emart24-restock",
        "url": "/"
    }

    response = webpush(
        subscription_info=subscription,
        data=json.dumps(payload, ensure_ascii=False),
        vapid_private_key=private_key,
        vapid_claims={"sub": subject},
        ttl=300
    )

    return response.status_code


# V9 PUSH OUTBOX SENDER 135-6-27
def process_push_outbox(limit=20):
    """발송 대기 중인 푸시 알림을 처리한다."""
    if not DATABASE_URL:
        return {"sent": 0, "failed": 0}

    limit = max(1, min(int(limit), 100))
    sent = 0
    failed = 0

    with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    o.id,
                    o.store_name,
                    o.quantity,
                    w.product_name,
                    s.endpoint,
                    s.p256dh,
                    s.auth
                FROM push_outbox AS o
                JOIN push_watch_items AS w ON w.id = o.watch_id
                JOIN push_subscriptions AS s ON s.id = w.subscription_id
                WHERE o.status = 'pending'
                  AND o.attempts < 3
                ORDER BY o.created_at, o.id
                LIMIT %s
                FOR UPDATE OF o SKIP LOCKED
            """, (limit,))

            rows = cur.fetchall()

            for row in rows:
                outbox_id, store_name, quantity, product_name, endpoint, p256dh, auth = row

                subscription = {
                    "endpoint": endpoint,
                    "keys": {
                        "p256dh": p256dh,
                        "auth": auth
                    }
                }

                try:
                    status_code = send_restock_push(
                        subscription,
                        product_name,
                        store_name,
                        quantity
                    )

                    if not 200 <= status_code < 300:
                        raise RuntimeError("푸시 응답 코드: " + str(status_code))

                    cur.execute("""
                        UPDATE push_outbox
                        SET status = 'sent',
                            sent_at = NOW(),
                            attempts = attempts + 1,
                            last_error = NULL
                        WHERE id = %s
                    """, (outbox_id,))

                    sent += 1

                except Exception as e:
                    cur.execute("""
                        UPDATE push_outbox
                        SET attempts = attempts + 1,
                            status = CASE
                                WHEN attempts + 1 >= 3 THEN 'failed'
                                ELSE 'pending'
                            END,
                            last_error = %s
                        WHERE id = %s
                    """, (type(e).__name__, outbox_id))

                    failed += 1

            conn.commit()

    return {"sent": sent, "failed": failed}


# V9 PUSH RUNNER 135-6-28
def run_push_notification_cycle():
    """재고 감시와 푸시 발송을 순서대로 실행한다."""
    if not DATABASE_URL:
        return {"status": "database_unavailable"}

    lock_key = 135628

    with psycopg.connect(DATABASE_URL, connect_timeout=5) as lock_conn:
        lock_conn.autocommit = True

        with lock_conn.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (lock_key,)
            )
            acquired = cur.fetchone()[0]

        if not acquired:
            return {"status": "already_running"}

        try:
            monitor_result = run_push_stock_monitor(limit=100)
            send_result = process_push_outbox(limit=100)

            return {
                "status": "completed",
                "monitor": monitor_result,
                "push": send_result
            }
        finally:
            with lock_conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (lock_key,)
                )
