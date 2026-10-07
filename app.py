import json
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

def load_history():
    try:
        return json.loads(HISTORY_FILE.read_text())
    except:
        return {"products":{}}

def save_history(data):
    HISTORY_FILE.write_text(json.dumps(data,ensure_ascii=False,indent=2))

def track_stock(plu, stores):
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
@app.get("/api/products")
def products(q:str):
    try:return {"products":search_products(q)}
    except Exception as e:raise HTTPException(502,str(e))
@app.get("/api/inventory")
def inventory(plu:str,area1="서울특별시",area2="광진구"):
    try:return {"stores":get_inventory(plu,area1,area2)}
    except Exception as e:raise HTTPException(502,str(e))
