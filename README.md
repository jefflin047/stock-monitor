# Stock Monitor

個人用股市監控 + 吉他學習工具，以 Flask 建構的輕量網頁應用。

## 功能

### 股市監控
- 自訂觀察清單，支援台股（TW）與美股（US）
- 即時股價、漲跌幅、成交量（透過 yfinance）
- 台股三大法人買賣超（TWSE T86）
- Google News RSS 最新相關新聞
- 每 15 分鐘自動背景刷新

### 吉他學習
- **和弦庫**：常用和弦指法圖與說明
- **刷弦練習**：節拍器 + 自訂刷弦節奏
- **搜尋吉他譜**：串接 Songsterr API 搜尋曲譜
- **初學者歌單**：推薦入門曲目含和弦標示
- **我的歌單**：書籤收藏練習曲，標記學習進度
- **我的樂譜**：上傳並管理個人樂譜檔案（PDF、圖片等）
- **練習視頻**：快速前往 YouTube、Bilibili 等平台搜尋教學
- **流行教學**：收藏流行歌曲教學網站，可自由新增／刪除

## 安裝與啟動

```bash
pip3 install -r requirements.txt
python3 main.py
```

開啟瀏覽器前往 http://localhost:5001

## 技術

- Python / Flask
- yfinance、feedparser、requests
- Bootstrap 5、Bootstrap Icons
- 資料以 JSON 檔儲存於本地
