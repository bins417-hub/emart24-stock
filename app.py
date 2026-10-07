import time
import json
import os
import psycopg
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
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
    r=s.get(WEB+"/api1/store",params={"page":1,"AREA1":a1,"AREA2":a2},timeout=20)
    r.raise_for_status()
    return [str(x["CODE"]).strip() for x in r.json().get("data",[])
            if x.get("CODE") and str(x.get("USE_YN","Y")).upper()=="Y"]

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

def get_inventory(plu,a1,a2):
    codes=get_store_codes(a1,a2)
    s.post(BASE+"/api/stock/v1/search/keyword",
        data={"pluCd":plu,"searchPage":"STOCK_SEARCH_SERVICE"},
        headers={"Content-Type":"application/x-www-form-urlencoded; charset=UTF-8"},timeout=20)
    info,qty={},{}
    for i in range(0,len(codes),20):
        r=s.get(BASE+"/api/stock/v2/stock-search/store",
            params={"searchPluCode":plu,"bizNoArr":",".join(codes[i:i+20])},timeout=20)
        r.raise_for_status()
        body=r.json()
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

    except Exception as e:
        raise HTTPException(502,str(e))
