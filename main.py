import json
import os
import re
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Optional

import feedparser
import requests
import yfinance as yf
from flask import Flask, jsonify, render_template, request, send_from_directory
from werkzeug.utils import secure_filename

app = Flask(__name__)

WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "watchlist.json")
CACHE_FILE = os.path.join(os.path.dirname(__file__), "cache.json")
POSITIONS_FILE = os.path.join(os.path.dirname(__file__), "positions.json")
RECIPES_FILE = os.path.join(os.path.dirname(__file__), "recipes.json")
RESTAURANTS_FILE = os.path.join(os.path.dirname(__file__), "restaurants.json")
RECIPE_PHOTOS_DIR = os.path.join(os.path.dirname(__file__), "uploads", "recipe_photos")
ALLOWED_PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
os.makedirs(RECIPE_PHOTOS_DIR, exist_ok=True)
REFRESH_INTERVAL = 15 * 60  # 15 minutes

TABS_DIR = os.path.join(os.path.dirname(__file__), "uploads", "tabs")
TABS_META_FILE = os.path.join(os.path.dirname(__file__), "tabs.json")
ALLOWED_TAB_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".txt", ".tab"}
MAX_TAB_SIZE = 20 * 1024 * 1024  # 20 MB
os.makedirs(TABS_DIR, exist_ok=True)

cache_data: dict = {}
cache_lock = threading.Lock()
twse_institutional_cache: dict = {}  # date -> full TWSE T86 data
tw_stock_cache: dict = {}            # ticker -> Chinese name (populated from TWSE APIs)


# ── Watchlist helpers ──────────────────────────────────────────────────────────

def load_watchlist() -> list:
    if os.path.exists(WATCHLIST_FILE):
        with open(WATCHLIST_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_watchlist(watchlist: list) -> None:
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(watchlist, f, ensure_ascii=False, indent=2)


def load_positions() -> list:
    if os.path.exists(POSITIONS_FILE):
        with open(POSITIONS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_positions(positions: list) -> None:
    with open(POSITIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(positions, f, ensure_ascii=False, indent=2)


def load_recipes() -> list:
    if os.path.exists(RECIPES_FILE):
        with open(RECIPES_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_recipes(recipes: list) -> None:
    with open(RECIPES_FILE, "w", encoding="utf-8") as f:
        json.dump(recipes, f, ensure_ascii=False, indent=2)


def load_restaurants() -> list:
    if os.path.exists(RESTAURANTS_FILE):
        with open(RESTAURANTS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_restaurants(restaurants: list) -> None:
    with open(RESTAURANTS_FILE, "w", encoding="utf-8") as f:
        json.dump(restaurants, f, ensure_ascii=False, indent=2)


def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_cache(data: dict) -> None:
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── Tabs (user uploads) ────────────────────────────────────────────────────────

def load_tabs_meta() -> list:
    if os.path.exists(TABS_META_FILE):
        with open(TABS_META_FILE, encoding="utf-8") as f:
            return json.load(f)
    return []


def save_tabs_meta(data: list) -> None:
    with open(TABS_META_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── Stock data ─────────────────────────────────────────────────────────────────

def get_stock_data(ticker: str, market: str) -> Optional[dict]:
    try:
        symbol = f"{ticker}.TW" if market == "TW" else ticker
        yf_ticker = yf.Ticker(symbol)
        hist = yf_ticker.history(period="5d")
        if hist.empty:
            return None

        today = hist.iloc[-1]
        prev = hist.iloc[-2] if len(hist) > 1 else today

        price = today["Close"]
        change = price - prev["Close"]
        change_pct = (change / prev["Close"]) * 100 if prev["Close"] else 0
        volume = today["Volume"]

        return {
            "price": round(float(price), 2),
            "change": round(float(change), 2),
            "change_pct": round(float(change_pct), 2),
            "volume": int(volume),
        }
    except Exception as e:
        print(f"[stock] {ticker}: {e}")
        return None


# ── TWSE 三大法人 ───────────────────────────────────────────────────────────────

def _fetch_twse_t86(target_date: str) -> Optional[dict]:
    """Fetch full T86 table for a given date string (YYYYMMDD). Returns dict keyed by ticker."""
    global twse_institutional_cache
    if target_date in twse_institutional_cache:
        return twse_institutional_cache[target_date]

    headers = {"User-Agent": "Mozilla/5.0"}
    url = (
        f"https://www.twse.com.tw/rwd/zh/fund/T86"
        f"?response=json&date={target_date}&selectType=ALLBUT0999"
    )
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        payload = resp.json()
        if payload.get("stat") != "OK":
            return None

        result = {}
        for row in payload.get("data", []):
            code = row[0].strip()
            name = row[1].strip()
            tw_stock_cache[code] = name  # populate name lookup cache
            result[code] = {
                "foreign_net": row[4].replace(",", "").replace("+", ""),
                "trust_net": row[7].replace(",", "").replace("+", ""),
                "dealer_net": row[10].replace(",", "").replace("+", ""),
                "total_net": row[11].replace(",", "").replace("+", ""),
            }
        twse_institutional_cache[target_date] = result
        return result
    except Exception as e:
        print(f"[twse] {target_date}: {e}")
        return None


def get_institutional(ticker: str) -> Optional[dict]:
    # Try today first, then walk back up to 5 trading days
    for offset in range(5):
        d = date.today() - timedelta(days=offset)
        date_str = d.strftime("%Y%m%d")
        table = _fetch_twse_t86(date_str)
        if table and ticker in table:
            return {**table[ticker], "date": d.strftime("%Y/%m/%d")}
    return None


# ── News ───────────────────────────────────────────────────────────────────────

def get_news(name: str, market: str) -> list:
    try:
        if market == "TW":
            url = (
                f"https://news.google.com/rss/search"
                f"?q={requests.utils.quote(name)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
            )
        else:
            url = (
                f"https://news.google.com/rss/search"
                f"?q={requests.utils.quote(name + ' stock')}&hl=en-US&gl=US&ceid=US:en"
            )
        feed = feedparser.parse(url)
        return [
            {
                "title": e.title,
                "link": e.link,
                "published": e.get("published", ""),
            }
            for e in feed.entries[:4]
        ]
    except Exception as e:
        print(f"[news] {name}: {e}")
        return []


# ── Main refresh ───────────────────────────────────────────────────────────────

def fetch_all_data() -> dict:
    watchlist = load_watchlist()
    result = {}
    for company in watchlist:
        ticker = company["ticker"]
        name = company["name"]
        market = company["market"]
        result[ticker] = {
            "ticker": ticker,
            "name": name,
            "market": market,
            "stock": get_stock_data(ticker, market),
            "institutional": get_institutional(ticker) if market == "TW" else None,
            "news": get_news(name, market),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
    save_cache(result)
    return result


def background_refresh():
    global cache_data
    while True:
        print(f"[{datetime.now():%H:%M}] Refreshing all data...")
        data = fetch_all_data()
        with cache_lock:
            cache_data = data
        time.sleep(REFRESH_INTERVAL)


def _load_tw_stock_list():
    """Load full TWSE + OTC company list into tw_stock_cache on startup."""
    sources = [
        "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",           # 上市
        "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes",   # 上櫃
    ]
    loaded = 0
    for url in sources:
        try:
            resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            for item in resp.json():
                code = str(item.get("Code", item.get("SecuritiesCompanyCode", ""))).strip()
                name = str(item.get("Name", item.get("CompanyName", ""))).strip()
                if code and name and re.match(r"^\d{4,6}$", code):
                    tw_stock_cache[code] = name
                    loaded += 1
        except Exception as e:
            print(f"[tw-list] {url}: {e}")
    print(f"[tw-list] Loaded {len(tw_stock_cache)} TW stocks")


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/guitar")
def guitar():
    return render_template("guitar.html")


def _search_songsterr(q: str) -> list:
    url = f"https://www.songsterr.com/api/songs?pattern={requests.utils.quote(q)}&size=12"
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
    results = []
    for s in resp.json():
        song_id = s.get("songId")
        title = s.get("title", "")
        artist = s.get("artist", "")
        slug = title.lower().replace(" ", "-").replace("'", "").replace("(", "").replace(")", "")
        results.append({
            "title": title,
            "artist": artist,
            "source": "Songsterr",
            "url": f"https://www.songsterr.com/a/wsa/{slug}-tabs-s{song_id}",
        })
    return results


@app.route("/api/guitar/search")
def guitar_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    try:
        return jsonify(_search_songsterr(q))
    except Exception as e:
        print(f"[guitar search] {e}")
        return jsonify([])


@app.route("/api/stock/lookup")
def api_stock_lookup():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "empty"}), 400

    # ── Taiwan: 4-6 digit ticker ──────────────────────────────────────────────
    if re.match(r"^\d{4,6}$", q):
        name = tw_stock_cache.get(q, "")
        if not name:
            # Not in cache — try yfinance as fallback
            try:
                info = yf.Ticker(f"{q}.TW").info
                name = info.get("longName") or info.get("shortName", "")
            except Exception:
                pass
        return jsonify({"ticker": q, "name": name or q, "market": "TW", "found": bool(name)})

    # ── Chinese text: search TW by name substring ─────────────────────────────
    if re.search(r"[一-鿿]", q):
        matches = [(k, v) for k, v in tw_stock_cache.items() if q in v]
        if matches:
            results = [{"ticker": k, "name": v, "market": "TW"} for k, v in matches[:8]]
            return jsonify(results)
        return jsonify([])

    # ── English / US ticker ────────────────────────────────────────────────────
    q_upper = q.upper()
    try:
        info = yf.Ticker(q_upper).info
        name = info.get("shortName") or info.get("longName", q_upper)
        # Verify it's a real security (has a market price)
        if info.get("regularMarketPrice") or info.get("currentPrice") or info.get("navPrice"):
            return jsonify({"ticker": q_upper, "name": name, "market": "US", "found": True})
    except Exception:
        pass
    # Return with whatever the user typed so they can still add manually
    return jsonify({"ticker": q_upper, "name": q_upper, "market": "US", "found": False})


@app.route("/api/watchlist", methods=["GET"])
def api_watchlist_get():
    return jsonify(load_watchlist())


@app.route("/api/watchlist", methods=["POST"])
def api_watchlist_add():
    data = request.get_json()
    ticker = data.get("ticker", "").strip().upper()
    name = data.get("name", "").strip()
    market = data.get("market", "TW")
    featured = data.get("featured", False)

    if not ticker or not name:
        return jsonify({"error": "ticker 和 name 不能空白"}), 400

    watchlist = load_watchlist()
    existing = next((c for c in watchlist if c["ticker"] == ticker), None)
    if existing:
        existing["featured"] = featured
        existing["name"] = name
        save_watchlist(watchlist)
        return jsonify({"success": True})

    watchlist.append({"ticker": ticker, "name": name, "market": market, "featured": featured})
    save_watchlist(watchlist)

    def refresh_one():
        global cache_data
        entry = {
            "ticker": ticker,
            "name": name,
            "market": market,
            "stock": get_stock_data(ticker, market),
            "institutional": get_institutional(ticker) if market == "TW" else None,
            "news": get_news(name, market),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        with cache_lock:
            cache_data[ticker] = entry
        save_cache(cache_data)

    threading.Thread(target=refresh_one, daemon=True).start()
    return jsonify({"success": True})


@app.route("/api/watchlist/<ticker>", methods=["DELETE"])
def api_watchlist_delete(ticker):
    ticker = ticker.upper()
    watchlist = [c for c in load_watchlist() if c["ticker"] != ticker]
    save_watchlist(watchlist)
    with cache_lock:
        cache_data.pop(ticker, None)
    save_cache(cache_data)
    return jsonify({"success": True})


@app.route("/api/data")
def api_data():
    with cache_lock:
        return jsonify(list(cache_data.values()))


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    global cache_data
    data = fetch_all_data()
    with cache_lock:
        cache_data = data
    return jsonify({"success": True, "count": len(data)})


# ── My Tabs routes ─────────────────────────────────────────────────────────────

def _save_tab_files(tab_id: str, files, start_index: int = 0) -> tuple:
    """Save uploaded files to disk. Returns (saved_list, error_str)."""
    saved = []
    for i, file in enumerate(files):
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ALLOWED_TAB_EXTS:
            return None, f"不支援的格式：{file.filename}"
        file.seek(0, 2)
        size = file.tell()
        file.seek(0)
        if size > MAX_TAB_SIZE:
            return None, f"檔案太大（{file.filename}），上限 20MB"
        stored = f"{tab_id}_{start_index + i}_{secure_filename(file.filename)}"
        file.save(os.path.join(TABS_DIR, stored))
        saved.append({"filename": stored, "original_name": file.filename,
                      "ext": ext, "size": size})
    return saved, None


@app.route("/api/tabs", methods=["GET"])
def api_tabs_list():
    return jsonify(load_tabs_meta())


@app.route("/api/tabs/upload", methods=["POST"])
def api_tabs_upload():
    files = request.files.getlist("files")
    title = request.form.get("title", "").strip()
    artist = request.form.get("artist", "").strip()
    note = request.form.get("note", "").strip()

    if not files or not files[0].filename:
        return jsonify({"error": "請選擇至少一個檔案"}), 400
    if not title:
        return jsonify({"error": "請填寫樂譜名稱"}), 400

    tab_id = uuid.uuid4().hex[:8]
    saved, err = _save_tab_files(tab_id, files)
    if err:
        return jsonify({"error": err}), 400

    meta = load_tabs_meta()
    meta.append({"id": tab_id, "title": title, "artist": artist, "note": note,
                 "files": saved, "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M")})
    save_tabs_meta(meta)
    return jsonify({"success": True, "id": tab_id})


@app.route("/api/tabs/<tab_id>/add", methods=["POST"])
def api_tabs_add_files(tab_id):
    meta = load_tabs_meta()
    item = next((t for t in meta if t["id"] == tab_id), None)
    if not item:
        return jsonify({"error": "找不到"}), 404

    files = request.files.getlist("files")
    if not files or not files[0].filename:
        return jsonify({"error": "請選擇檔案"}), 400

    saved, err = _save_tab_files(tab_id, files, start_index=len(item.get("files", [])))
    if err:
        return jsonify({"error": err}), 400

    item.setdefault("files", []).extend(saved)
    save_tabs_meta(meta)
    return jsonify({"success": True})


@app.route("/api/tabs/<tab_id>", methods=["DELETE"])
def api_tabs_delete(tab_id):
    meta = load_tabs_meta()
    item = next((t for t in meta if t["id"] == tab_id), None)
    if not item:
        return jsonify({"error": "找不到"}), 404
    for f in item.get("files", []):
        fp = os.path.join(TABS_DIR, f["filename"])
        if os.path.exists(fp):
            os.remove(fp)
    save_tabs_meta([t for t in meta if t["id"] != tab_id])
    return jsonify({"success": True})


@app.route("/api/tabs/<tab_id>/files/<path:filename>", methods=["DELETE"])
def api_tabs_delete_file(tab_id, filename):
    meta = load_tabs_meta()
    item = next((t for t in meta if t["id"] == tab_id), None)
    if not item:
        return jsonify({"error": "找不到"}), 404
    fp = os.path.join(TABS_DIR, filename)
    if os.path.exists(fp):
        os.remove(fp)
    item["files"] = [f for f in item.get("files", []) if f["filename"] != filename]
    save_tabs_meta(meta)
    return jsonify({"success": True})


@app.route("/tabs/<tab_id>")
def tabs_view(tab_id):
    meta = load_tabs_meta()
    item = next((t for t in meta if t["id"] == tab_id), None)
    if not item:
        return "Not found", 404
    return render_template("tab_view.html", tab=item)


@app.route("/tabs/<tab_id>/files/<path:filename>")
def tabs_serve_file(tab_id, filename):
    return send_from_directory(TABS_DIR, filename)


@app.route("/portfolio")
def portfolio():
    return render_template("portfolio.html")


@app.route("/api/positions", methods=["GET"])
def api_positions_get():
    return jsonify(load_positions())


@app.route("/api/positions", methods=["POST"])
def api_positions_add():
    data = request.get_json()
    ticker = data.get("ticker", "").strip().upper()
    name = data.get("name", "").strip()
    market = data.get("market", "TW")
    cost = float(data.get("cost", 0))
    shares = int(data.get("shares", 0))

    if not ticker or not name:
        return jsonify({"error": "ticker 和 name 不能空白"}), 400
    if cost <= 0 or shares <= 0:
        return jsonify({"error": "成本和張數必須大於 0"}), 400

    positions = load_positions()
    if any(p["ticker"] == ticker for p in positions):
        return jsonify({"error": f"{ticker} 已在持倉中"}), 400

    positions.append({"ticker": ticker, "name": name, "market": market, "cost": cost, "shares": shares})
    save_positions(positions)
    return jsonify({"success": True})


@app.route("/api/positions/<ticker>", methods=["DELETE"])
def api_positions_delete(ticker):
    ticker = ticker.upper()
    save_positions([p for p in load_positions() if p["ticker"] != ticker])
    return jsonify({"success": True})


@app.route("/api/portfolio/prices")
def api_portfolio_prices():
    result = {}
    for pos in load_positions():
        ticker = pos["ticker"]
        data = get_stock_data(ticker, pos.get("market", "TW"))
        if data:
            result[ticker] = data
    return jsonify(result)


_ZH_RECIPES = [
    {"name":"番茄炒蛋","main_ingredient":"番茄","category":"家常菜",
     "ingredients":["番茄 2顆","雞蛋 3顆","鹽 1小匙","糖 半小匙","蔥花 少許","食用油 適量"],
     "steps":["番茄切塊，雞蛋打散加少許鹽","熱鍋下油炒蛋至半熟盛起","原鍋爆香蔥白，放入番茄大火炒出汁","加鹽、糖調味，放回炒蛋翻拌","撒蔥花起鍋"],
     "notes":"番茄要炒出汁才夠味，糖可依口味增減","photo_url":"","social_links":[]},
    {"name":"三杯雞","main_ingredient":"雞腿","category":"台式料理",
     "ingredients":["雞腿 600g","老薑片 30g","大蒜 10瓣","九層塔 一把","麻油 3大匙","醬油 3大匙","米酒 3大匙","糖 1大匙"],
     "steps":["雞腿切塊，薑切片，蒜去皮","麻油小火爆香薑片至金黃","放入大蒜炒香，加雞塊大火翻炒上色","加醬油、米酒、糖翻炒均勻","蓋蓋中火燜8分鐘至收汁","起鍋前放九層塔大火翻炒即可"],
     "notes":"麻油不可高溫久炸；九層塔最後加保留香氣","photo_url":"","social_links":[]},
    {"name":"紅燒肉","main_ingredient":"五花肉","category":"家常菜",
     "ingredients":["五花肉 600g","醬油 4大匙","冰糖 2大匙","米酒 3大匙","蔥 2根","薑片 3片","八角 2顆","水 適量"],
     "steps":["五花肉切塊，冷水入鍋煮滾撈起洗淨","鍋中放冰糖小火炒至琥珀色","放入肉塊翻炒上色","加醬油、米酒、蔥、薑、八角，加水至蓋過肉","大火煮滾轉小火燉40分鐘","開蓋大火收汁至濃稠"],
     "notes":"冰糖焦化後顏色更紅亮；燉煮時間越長越入味","photo_url":"","social_links":[]},
    {"name":"宮保雞丁","main_ingredient":"雞胸肉","category":"川菜",
     "ingredients":["雞胸肉 300g","花生 50g","乾辣椒 8根","花椒 1小匙","蔥段 適量","醬油 1大匙","醋 1大匙","糖 1大匙","太白粉 1小匙","米酒 1大匙","鹽 少許"],
     "steps":["雞胸切丁，加醬油、米酒、太白粉醃10分鐘","混合醬汁：醬油、醋、糖、太白粉水備用","熱油鍋爆香花椒、乾辣椒","放入雞丁大火翻炒至熟","加蔥段翻炒，倒入醬汁快速翻炒","最後加花生炒勻即可"],
     "notes":"醋讓口味更有層次；花生最後加才能保持脆感","photo_url":"","social_links":[]},
    {"name":"麻婆豆腐","main_ingredient":"豆腐","category":"川菜",
     "ingredients":["嫩豆腐 1盒","豬絞肉 100g","豆瓣醬 2大匙","蒜末 2瓣","薑末 少許","醬油 1大匙","花椒粉 1小匙","太白粉水 適量","蔥花 少許","食用油 適量"],
     "steps":["豆腐切丁，用鹽水泡5分鐘備用","熱鍋下油炒香蒜末、薑末","放入豬絞肉炒熟","加豆瓣醬炒出紅油","加醬油和適量水煮滾，放入豆腐輕輕翻炒","用太白粉水勾芡，撒花椒粉和蔥花"],
     "notes":"豆腐先鹽水泡過比較不易碎","photo_url":"","social_links":[]},
    {"name":"滷肉飯","main_ingredient":"五花肉","category":"台式料理",
     "ingredients":["五花肉 500g","紅蔥頭 8顆","醬油 5大匙","冰糖 1大匙","米酒 2大匙","五香粉 少許","白胡椒 少許","水 300ml","白飯 適量"],
     "steps":["五花肉切小丁或條狀","紅蔥頭切片，熱油炸至金黃撈起","原鍋炒豬肉至出油上色","加入炸紅蔥頭、醬油、冰糖、米酒、五香粉","加水大火煮滾轉小火燉40分鐘","收汁後淋在白飯上"],
     "notes":"紅蔥頭是靈魂，不可省略；燉越久越香","photo_url":"","social_links":[]},
    {"name":"清炒空心菜","main_ingredient":"空心菜","category":"家常菜",
     "ingredients":["空心菜 300g","大蒜 3瓣","辣椒 1根（可省）","鹽 適量","食用油 適量"],
     "steps":["空心菜洗淨切段，蒜切末","熱鍋下油爆香蒜末和辣椒","大火放入空心菜迅速翻炒","加鹽調味炒至軟身即可起鍋"],
     "notes":"大火快炒才能保留脆嫩口感","photo_url":"","social_links":[]},
    {"name":"蒸蛋","main_ingredient":"雞蛋","category":"家常菜",
     "ingredients":["雞蛋 3顆","高湯或水 200ml（蛋液的1.5倍）","鹽 少許","醬油 少許","蔥花 少許","芝麻油 少許"],
     "steps":["雞蛋打散，加鹽輕輕攪拌均勻","加入高湯（溫熱）攪拌","過篩去除氣泡","覆蓋保鮮膜或鋁箔紙","中小火蒸12-15分鐘","淋上醬油和芝麻油，撒蔥花"],
     "notes":"加溫熱高湯且過篩，蒸出來才細嫩光滑","photo_url":"","social_links":[]},
    {"name":"香菇雞湯","main_ingredient":"雞肉","category":"湯品",
     "ingredients":["雞腿 2支","乾香菇 8朵","薑片 3片","米酒 2大匙","鹽 適量","蔥段 適量","水 1500ml"],
     "steps":["乾香菇泡發，剪去蒂頭；雞腿剁塊汆燙洗淨","鍋中放雞肉、香菇、薑片，加水大火煮滾","撈去浮沫，加米酒","轉小火燉40分鐘","加鹽調味，撒蔥段"],
     "notes":"泡香菇的水過濾後加入湯中，味道更鮮","photo_url":"","social_links":[]},
    {"name":"糖醋排骨","main_ingredient":"排骨","category":"家常菜",
     "ingredients":["排骨 500g","醬油 2大匙","醋 3大匙","糖 3大匙","番茄醬 2大匙","蒜末 2瓣","太白粉 適量","食用油 適量","鹽 少許"],
     "steps":["排骨加醬油、太白粉醃20分鐘","下鍋炸至金黃撈起","鍋留少許油爆香蒜末","加醋、糖、番茄醬、少許水調成醬汁煮滾","放入排骨大火翻炒至醬汁收稠"],
     "notes":"醋要最後加才能保留酸味；比例可依個人口味調整","photo_url":"","social_links":[]},
    {"name":"蔥爆牛肉","main_ingredient":"牛肉","category":"家常菜",
     "ingredients":["牛肉片 300g","蔥 4根","醬油 2大匙","米酒 1大匙","太白粉 1小匙","糖 少許","薑末 少許","食用油 適量"],
     "steps":["牛肉片加醬油、米酒、太白粉醃15分鐘","蔥切段備用","熱鍋大火下油，牛肉片快速炒至變色盛起","原鍋爆香蔥白段","放回牛肉加蔥綠翻炒均勻加糖調味"],
     "notes":"牛肉要大火快炒才嫩；醃製時可加少許小蘇打","photo_url":"","social_links":[]},
    {"name":"魚香茄子","main_ingredient":"茄子","category":"川菜",
     "ingredients":["茄子 2條","豬絞肉 80g","蒜末 3瓣","薑末 少許","蔥花 少許","豆瓣醬 1大匙","醬油 1大匙","醋 1大匙","糖 1小匙","太白粉水 適量"],
     "steps":["茄子切條，用鹽水泡10分鐘後擠乾","熱油鍋將茄子煎至軟化盛起","原鍋炒香蒜薑末，下絞肉炒熟","加豆瓣醬炒出紅油，加醬油醋糖調味","放回茄子翻炒均勻，用太白粉水勾薄芡","撒蔥花起鍋"],
     "notes":"茄子鹽水泡過不易氧化變黑","photo_url":"","social_links":[]},
    {"name":"清蒸魚","main_ingredient":"魚","category":"家常菜",
     "ingredients":["鱸魚 1條（約500g）","薑絲 適量","蔥絲 適量","辣椒絲 少許","蒸魚醬油 3大匙","熱油 適量"],
     "steps":["魚洗淨在魚身兩側各劃3刀","放上薑片，大火蒸8-10分鐘","倒掉多餘湯汁","鋪上蔥絲辣椒絲","淋上蒸魚醬油","燒熱油潑在蔥絲上即可"],
     "notes":"蒸的時間不可太長，否則魚肉老硬；油一定要夠熱","photo_url":"","social_links":[]},
    {"name":"皮蛋豆腐","main_ingredient":"豆腐","category":"涼菜",
     "ingredients":["嫩豆腐 1盒","皮蛋 2顆","蔥花 少許","薑末 少許","醬油膏 2大匙","芝麻油 1小匙","辣油 少許（可省）"],
     "steps":["豆腐切塊擺盤","皮蛋剝殼切丁鋪在豆腐上","混合醬油膏、薑末、芝麻油調成醬汁淋上","撒蔥花，可加辣油"],
     "notes":"豆腐冷藏後口感更佳；夏天快速料理首選","photo_url":"","social_links":[]},
    {"name":"蔥油雞","main_ingredient":"雞腿","category":"台式料理",
     "ingredients":["雞腿 2支","蔥 4根","薑片 3片","米酒 2大匙","鹽 適量","芝麻油 1大匙","食用油 3大匙"],
     "steps":["鍋中放水、薑片、米酒、鹽煮滾","放入雞腿，中火煮15分鐘後關火","蓋蓋悶10分鐘，取出放涼切塊","蔥切細末，鋪在雞塊上","熱油加芝麻油燒至冒煙，淋在蔥末上","淋上少許醬油即可"],
     "notes":"關火後悶熟可避免雞肉過老；蔥末要細才能被熱油激出香味","photo_url":"","social_links":[]},
    {"name":"炒飯","main_ingredient":"白飯","category":"家常菜",
     "ingredients":["冷白飯 2碗","雞蛋 2顆","蔥花 適量","鹽 適量","醬油 1大匙","食用油 適量","配料（火腿、玉米、蝦仁等）隨意"],
     "steps":["蛋打散，熱鍋大火下油炒蛋至半熟盛起","原鍋補油，冷飯下鍋大火翻炒散開","加鹽、醬油調味翻炒","放回炒蛋和配料翻炒均勻","撒蔥花起鍋"],
     "notes":"一定要用冷飯（最好是隔夜飯），飯粒才乾鬆不黏","photo_url":"","social_links":[]},
    {"name":"紅燒獅子頭","main_ingredient":"豬絞肉","category":"家常菜",
     "ingredients":["豬絞肉 500g","荸薺 6顆","薑末 1小匙","蔥末 2大匙","醬油 3大匙","糖 1大匙","米酒 1大匙","太白粉 2大匙","雞蛋 1顆","大白菜 半顆"],
     "steps":["荸薺切碎，加入絞肉、薑蔥末、醬油、糖、太白粉、蛋混合均勻","用手捏出4顆大肉丸","熱油鍋將肉丸煎至各面金黃","鍋底鋪大白菜，放上肉丸","加醬油、糖、水（蓋過一半），蓋蓋小火燉40分鐘"],
     "notes":"荸薺增加口感；燉煮過程不要翻動肉丸","photo_url":"","social_links":[]},
    {"name":"酸辣湯","main_ingredient":"豆腐","category":"湯品",
     "ingredients":["嫩豆腐 半盒","木耳 30g","金針菇 50g","蛋 2顆","豬血糕或豬肉絲 適量","醬油 2大匙","烏醋 3大匙","白胡椒 1小匙","太白粉水 適量","高湯 1000ml","鹽 適量"],
     "steps":["高湯煮滾，放入木耳、金針菇、肉絲煮熟","加豆腐丁，調入醬油、鹽、白胡椒","用太白粉水勾芡至濃稠","蛋打散，慢慢倒入攪拌成蛋花","起鍋前加烏醋，撒香菜"],
     "notes":"烏醋起鍋前才加，酸味才明顯；白胡椒量要夠辣才好喝","photo_url":"","social_links":[]},
    {"name":"韓式泡菜炒豬肉","main_ingredient":"豬肉","category":"韓式料理",
     "ingredients":["豬五花肉片 300g","泡菜 200g","洋蔥 半顆","蒜末 2瓣","醬油 1大匙","糖 少許","芝麻油 少許","白芝麻 少許"],
     "steps":["洋蔥切絲，五花肉片切段","熱鍋不放油，五花肉片乾炒至出油上色","加蒜末翻炒香","放入泡菜和洋蔥大火翻炒","加醬油、糖調味","起鍋前淋芝麻油撒芝麻"],
     "notes":"五花肉自帶油脂，不需另外加油；泡菜要熟成的才好吃","photo_url":"","social_links":[]},
    {"name":"蚵仔煎","main_ingredient":"蚵仔","category":"台式小吃",
     "ingredients":["蚵仔 150g","地瓜粉 3大匙","太白粉 1大匙","水 120ml","雞蛋 2顆","韭菜或小白菜 適量","甜辣醬 適量"],
     "steps":["地瓜粉、太白粉加水調成粉漿","熱鍋下油放入蚵仔煎至微熟","倒入粉漿覆蓋蚵仔","蛋打在旁邊，翻面讓蛋在底部","放入蔬菜，再次翻面","盛盤淋上甜辣醬"],
     "notes":"粉漿不能太稠；火不能太大，邊緣才會脆","photo_url":"","social_links":[]},
]


def _search_zh_recipes(q: str) -> list:
    q = q.strip().lower()
    results = []
    for r in _ZH_RECIPES:
        if (q in r["name"].lower() or
                q in r["main_ingredient"].lower() or
                q in r["category"].lower() or
                any(q in ing.lower() for ing in r["ingredients"])):
            results.append(r)
    return results[:6]


@app.route("/api/search/recipe")
def api_search_recipe():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])

    # Chinese query → search local database
    if re.search(r"[一-鿿]", q):
        return jsonify(_search_zh_recipes(q))

    # English → TheMealDB
    try:
        resp = requests.get(
            "https://www.themealdb.com/api/json/v1/1/search.php",
            params={"s": q},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=8,
        )
        meals = resp.json().get("meals") or []
        results = []
        for meal in meals[:6]:
            ings = []
            for i in range(1, 21):
                ing     = (meal.get(f"strIngredient{i}") or "").strip()
                measure = (meal.get(f"strMeasure{i}")    or "").strip()
                if ing:
                    ings.append(f"{ing} {measure}".strip() if measure else ing)
            raw = meal.get("strInstructions", "")
            steps = [s.strip() for s in re.split(r"\r?\n", raw) if s.strip()]
            if len(steps) <= 2:
                steps = [s.strip() for s in re.split(r"(?<=[.!?])\s+", raw) if s.strip()]
            links = []
            src = meal.get("strSource", "")
            mid = meal.get("idMeal", "")
            if src:
                links.append({"label": "原始食譜", "url": src})
            if mid:
                links.append({"label": "TheMealDB", "url": f"https://www.themealdb.com/meal/{mid}"})
            results.append({
                "name":           meal.get("strMeal", ""),
                "main_ingredient":(meal.get("strIngredient1") or "").strip(),
                "category":       meal.get("strCategory", ""),
                "ingredients":    ings,
                "steps":          steps[:20],
                "notes":          "",
                "photo_url":      meal.get("strMealThumb", ""),
                "social_links":   links,
            })
        return jsonify(results)
    except Exception as e:
        print(f"[recipe search] {e}")
        return jsonify([])


@app.route("/api/search/restaurant")
def api_search_restaurant():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])

    results = []

    # Overpass API — richer tags (hours, cuisine, phone…)
    try:
        overpass_query = f"""
[out:json][timeout:12];
(
  node["name"~"{q}"]["amenity"](22.0,119.5,25.5,122.5);
  way["name"~"{q}"]["amenity"](22.0,119.5,25.5,122.5);
);
out center body 8;
"""
        resp = requests.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": overpass_query},
            headers={"User-Agent": "StockMonitorApp/1.0"},
            timeout=14,
        )
        for el in resp.json().get("elements", []):
            tags = el.get("tags", {})
            name = tags.get("name", "").strip()
            if not name:
                continue
            lat = el.get("lat") or (el.get("center") or {}).get("lat")
            lon = el.get("lon") or (el.get("center") or {}).get("lon")
            map_url = f"https://www.google.com/maps?q={lat},{lon}" if lat and lon else ""

            cuisine = tags.get("cuisine", "").replace(";", "、").replace("_", " ")
            hours   = tags.get("opening_hours", "")
            phone   = tags.get("phone") or tags.get("contact:phone", "")
            website = tags.get("website") or tags.get("contact:website", "")
            pay_parts = []
            if tags.get("payment:cash")         == "yes": pay_parts.append("現金")
            if tags.get("payment:credit_cards") == "yes" or tags.get("payment:visa") == "yes": pay_parts.append("信用卡")
            if tags.get("payment:debit_cards")  == "yes": pay_parts.append("金融卡")

            review_links = []
            if website:
                review_links.append({"label": "官方網站", "url": website})

            results.append({
                "name":        name,
                "cuisine":     cuisine,
                "hours":       hours,
                "reservation": f"電話 {phone}" if phone else "",
                "payment":     "、".join(pay_parts),
                "parking":     "",
                "map_url":     map_url,
                "my_notes":    "",
                "review_links": review_links,
            })
            if len(results) >= 6:
                break
    except Exception as e:
        print(f"[overpass] {e}")

    # Fall back to Nominatim if nothing found
    if not results:
        try:
            resp = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": q, "format": "json", "addressdetails": 1, "limit": 5},
                headers={"User-Agent": "StockMonitorApp/1.0"},
                timeout=8,
            )
            for place in resp.json():
                name = (place.get("name") or place.get("display_name", "").split(",")[0]).strip()
                if not name:
                    continue
                lat, lon = place.get("lat"), place.get("lon")
                map_url = f"https://www.google.com/maps?q={lat},{lon}" if lat and lon else ""
                results.append({
                    "name": name, "cuisine": "", "hours": "", "reservation": "",
                    "payment": "", "parking": "", "map_url": map_url,
                    "my_notes": "", "review_links": [],
                })
        except Exception as e:
            print(f"[nominatim] {e}")

    if not results:
        results.append({
            "name": q, "cuisine": "", "hours": "", "reservation": "",
            "payment": "", "parking": "",
            "map_url": f"https://www.google.com/maps/search/{requests.utils.quote(q)}",
            "my_notes": "", "review_links": [],
        })

    return jsonify(results)


@app.route("/food")
def food():
    return render_template("food.html")


# ── Recipes ────────────────────────────────────────────────────────────────────

@app.route("/api/recipes", methods=["GET"])
def api_recipes_get():
    return jsonify(load_recipes())


@app.route("/api/recipes", methods=["POST"])
def api_recipes_add():
    data = request.get_json()
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "菜名不能空白"}), 400
    recipe = {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "main_ingredient": data.get("main_ingredient", "").strip(),
        "category": data.get("category", "").strip(),
        "ingredients": data.get("ingredients", []),
        "steps": data.get("steps", []),
        "notes": data.get("notes", "").strip(),
        "social_links": data.get("social_links", []),
        "photos": [],
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    recipes = load_recipes()
    recipes.append(recipe)
    save_recipes(recipes)
    return jsonify({"success": True, "id": recipe["id"]})


@app.route("/api/recipes/<recipe_id>", methods=["PUT"])
def api_recipes_update(recipe_id):
    data = request.get_json()
    recipes = load_recipes()
    item = next((r for r in recipes if r["id"] == recipe_id), None)
    if not item:
        return jsonify({"error": "找不到"}), 404
    for field in ["name", "main_ingredient", "category", "notes"]:
        if field in data:
            item[field] = data[field]
    for field in ["ingredients", "steps", "social_links"]:
        if field in data:
            item[field] = data[field]
    save_recipes(recipes)
    return jsonify({"success": True})


@app.route("/api/recipes/<recipe_id>", methods=["DELETE"])
def api_recipes_delete(recipe_id):
    recipes = load_recipes()
    item = next((r for r in recipes if r["id"] == recipe_id), None)
    if item:
        for photo in item.get("photos", []):
            fp = os.path.join(RECIPE_PHOTOS_DIR, photo)
            if os.path.exists(fp):
                os.remove(fp)
    save_recipes([r for r in recipes if r["id"] != recipe_id])
    return jsonify({"success": True})


@app.route("/api/recipes/<recipe_id>/photos", methods=["POST"])
def api_recipes_upload_photo(recipe_id):
    recipes = load_recipes()
    item = next((r for r in recipes if r["id"] == recipe_id), None)
    if not item:
        return jsonify({"error": "找不到"}), 404
    files = request.files.getlist("photos")
    if not files or not files[0].filename:
        return jsonify({"error": "請選擇圖片"}), 400
    saved = []
    for i, file in enumerate(files):
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in ALLOWED_PHOTO_EXTS:
            return jsonify({"error": f"不支援的格式：{file.filename}"}), 400
        file.seek(0, 2)
        if file.tell() > MAX_TAB_SIZE:
            return jsonify({"error": "圖片太大，上限 20MB"}), 400
        file.seek(0)
        idx = len(item.get("photos", [])) + i
        stored = f"{recipe_id}_{idx}_{secure_filename(file.filename)}"
        file.save(os.path.join(RECIPE_PHOTOS_DIR, stored))
        saved.append(stored)
    item.setdefault("photos", []).extend(saved)
    save_recipes(recipes)
    return jsonify({"success": True, "photos": saved})


@app.route("/api/recipes/<recipe_id>/photos/<path:filename>", methods=["DELETE"])
def api_recipes_delete_photo(recipe_id, filename):
    recipes = load_recipes()
    item = next((r for r in recipes if r["id"] == recipe_id), None)
    if not item:
        return jsonify({"error": "找不到"}), 404
    fp = os.path.join(RECIPE_PHOTOS_DIR, filename)
    if os.path.exists(fp):
        os.remove(fp)
    item["photos"] = [p for p in item.get("photos", []) if p != filename]
    save_recipes(recipes)
    return jsonify({"success": True})


@app.route("/food/photos/<path:filename>")
def recipe_photo(filename):
    return send_from_directory(RECIPE_PHOTOS_DIR, filename)


# ── Restaurants ────────────────────────────────────────────────────────────────

@app.route("/api/restaurants", methods=["GET"])
def api_restaurants_get():
    return jsonify(load_restaurants())


@app.route("/api/restaurants", methods=["POST"])
def api_restaurants_add():
    data = request.get_json()
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "餐廳名稱不能空白"}), 400
    restaurant = {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "cuisine": data.get("cuisine", "").strip(),
        "reservation": data.get("reservation", "").strip(),
        "hours": data.get("hours", "").strip(),
        "payment": data.get("payment", "").strip(),
        "parking": data.get("parking", "").strip(),
        "map_url": data.get("map_url", "").strip(),
        "my_notes": data.get("my_notes", "").strip(),
        "rating": int(data.get("rating", 0)),
        "visited": bool(data.get("visited", False)),
        "review_links": data.get("review_links", []),
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    restaurants = load_restaurants()
    restaurants.append(restaurant)
    save_restaurants(restaurants)
    return jsonify({"success": True, "id": restaurant["id"]})


@app.route("/api/restaurants/<restaurant_id>", methods=["PUT"])
def api_restaurants_update(restaurant_id):
    data = request.get_json()
    restaurants = load_restaurants()
    item = next((r for r in restaurants if r["id"] == restaurant_id), None)
    if not item:
        return jsonify({"error": "找不到"}), 404
    for field in ["name", "cuisine", "reservation", "hours", "payment", "parking", "map_url", "my_notes"]:
        if field in data:
            item[field] = data[field]
    if "rating" in data:
        item["rating"] = int(data["rating"])
    if "visited" in data:
        item["visited"] = bool(data["visited"])
    if "review_links" in data:
        item["review_links"] = data["review_links"]
    save_restaurants(restaurants)
    return jsonify({"success": True})


@app.route("/api/restaurants/<restaurant_id>", methods=["DELETE"])
def api_restaurants_delete(restaurant_id):
    save_restaurants([r for r in load_restaurants() if r["id"] != restaurant_id])
    return jsonify({"success": True})


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cache_data = load_cache()
    threading.Thread(target=background_refresh, daemon=True).start()
    threading.Thread(target=_load_tw_stock_list, daemon=True).start()
    print("Server starting at http://localhost:5001")
    app.run(debug=False, port=5001, use_reloader=False)
