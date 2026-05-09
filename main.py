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

    if not ticker or not name:
        return jsonify({"error": "ticker 和 name 不能空白"}), 400

    watchlist = load_watchlist()
    if any(c["ticker"] == ticker for c in watchlist):
        return jsonify({"error": f"{ticker} 已在監控清單中"}), 400

    watchlist.append({"ticker": ticker, "name": name, "market": market})
    save_watchlist(watchlist)

    # Fetch data for the new company immediately in background
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


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cache_data = load_cache()
    threading.Thread(target=background_refresh, daemon=True).start()
    threading.Thread(target=_load_tw_stock_list, daemon=True).start()
    print("Server starting at http://localhost:5001")
    app.run(debug=False, port=5001, use_reloader=False)
