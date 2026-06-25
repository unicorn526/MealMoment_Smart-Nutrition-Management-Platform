# 食刻管理：Free / Premium 展示版

## 展示帳號

- Free 免費版：`free_demo` / `free123`
- Premium 付費版：`premium_demo` / `premium123`

## 主要差異

Free：
- 基本飲食紀錄
- 每日熱量計算
- 只使用資料庫已有食物
- 資料庫找不到時不呼叫 GPT

Premium：
- 資料庫優先查詢
- 資料庫查不到時才呼叫 GPT 估算
- 進階營養分析
- 一週熱量趨勢圖

## 執行

```bash
python -m venv venv
venv\Scripts\activate  # Windows
pip install -r requirements.txt
python app.py
```

開啟： http://127.0.0.1:5000
