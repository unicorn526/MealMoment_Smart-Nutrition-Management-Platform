from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from openai import OpenAI
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "shike.db"
load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-this-secret-key")

NUTRITION_SCHEMA = {
    "type": "object",
    "properties": {
        "food_name": {"type": "string"},
        "serving_description": {"type": "string"},
        "calories": {"type": "number"},
        "protein": {"type": "number"},
        "carbs": {"type": "number"},
        "fat": {"type": "number"},
        "sugar": {"type": "number"},
        "fiber": {"type": "number"},
        "sodium": {"type": "number"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "notes": {"type": "string"},
    },
    "required": ["food_name", "serving_description", "calories", "protein", "carbs", "fat", "sugar", "fiber", "sodium", "confidence", "notes"],
    "additionalProperties": False,
}

AI_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "positive_points": {
            "type": "array",
            "items": {"type": "string"}
        },
        "risk_points": {
            "type": "array",
            "items": {"type": "string"}
        },
        "suggestions": {
            "type": "array",
            "items": {"type": "string"}
        },
        "next_meal_suggestion": {"type": "string"},
    },
    "required": [
        "overview",
        "positive_points",
        "risk_points",
        "suggestions",
        "next_meal_suggestion",
    ],
    "additionalProperties": False,
}


def normalize_food_input(text: str) -> str:
    """把使用者輸入轉成穩定的查詢鍵。"""
    normalized = text.strip().lower()
    normalized = re.sub(r"\s+", "", normalized)
    normalized = normalized.replace("，", ",").replace("。", ".")
    normalized = re.sub(r"[、；;：:!！?？（）()\[\]{}『』「」\"'`~～\-_/\\]", "", normalized)
    return normalized


def get_plan_name(plan: str | None) -> str:
    return "Premium 付費版" if plan == "premium" else "Free 免費版"


def is_premium_user(user: sqlite3.Row | None) -> bool:
    return bool(user and user["plan"] == "premium")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(error: Exception | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def column_exists(db: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in db.execute(f"PRAGMA table_info({table})").fetchall())


def seed_demo_user(cursor: sqlite3.Cursor, username: str, password: str, plan: str, goal: str, daily_calorie_goal: int, protein_goal: int) -> None:
    existing = cursor.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    if existing is None:
        cursor.execute(
            """
            INSERT INTO users (username, password_hash, plan, goal, daily_calorie_goal, protein_goal)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (username, generate_password_hash(password), plan, goal, daily_calorie_goal, protein_goal),
        )
    else:
        cursor.execute(
            """
            UPDATE users
            SET plan = ?, goal = ?, daily_calorie_goal = ?, protein_goal = ?
            WHERE username = ?
            """,
            (plan, goal, daily_calorie_goal, protein_goal, username),
        )


def init_db() -> None:
    db = sqlite3.connect(DATABASE)
    cursor = db.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            plan TEXT DEFAULT 'free',
            height REAL DEFAULT 170,
            weight REAL DEFAULT 65,
            goal TEXT DEFAULT '維持健康',
            daily_calorie_goal INTEGER DEFAULT 1800,
            protein_goal INTEGER DEFAULT 70,
            sodium_limit INTEGER DEFAULT 2000,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS foods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            calories REAL NOT NULL,
            protein REAL DEFAULT 0,
            carbs REAL DEFAULT 0,
            fat REAL DEFAULT 0,
            sugar REAL DEFAULT 0,
            fiber REAL DEFAULT 0,
            sodium REAL DEFAULT 0,
            source TEXT DEFAULT 'manual',
            input_key TEXT DEFAULT NULL,
            estimate_json TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS food_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            food_id INTEGER NOT NULL,
            meal_type TEXT NOT NULL,
            quantity REAL DEFAULT 1,
            log_date TEXT NOT NULL,
            original_input TEXT DEFAULT '',
            note TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (food_id) REFERENCES foods(id)
        );
    """)

    if not column_exists(db, "users", "plan"):
        cursor.execute("ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'free'")
    if not column_exists(db, "foods", "source"):
        cursor.execute("ALTER TABLE foods ADD COLUMN source TEXT DEFAULT 'manual'")
    if not column_exists(db, "foods", "estimate_json"):
        cursor.execute("ALTER TABLE foods ADD COLUMN estimate_json TEXT DEFAULT ''")
    if not column_exists(db, "foods", "input_key"):
        cursor.execute("ALTER TABLE foods ADD COLUMN input_key TEXT DEFAULT NULL")
    if not column_exists(db, "food_logs", "original_input"):
        cursor.execute("ALTER TABLE food_logs ADD COLUMN original_input TEXT DEFAULT ''")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_foods_input_key ON foods(input_key) WHERE input_key IS NOT NULL")

    sample_foods = [
        ("白飯一碗", 280, 5, 62, 0.5, 0, 1, 5, "manual"),
        ("雞胸肉100g", 165, 31, 0, 3.6, 0, 0, 74, "manual"),
        ("水煮蛋一顆", 78, 6, 0.6, 5, 0.6, 0, 62, "manual"),
        ("蛋餅", 350, 12, 38, 17, 2, 2, 650, "manual"),
        ("雞腿便當", 780, 35, 95, 28, 10, 5, 1200, "manual"),
        ("沙拉", 220, 8, 20, 10, 6, 6, 350, "manual"),
        ("珍珠奶茶", 600, 8, 95, 18, 55, 0, 120, "manual"),
        ("無糖豆漿", 130, 10, 8, 6, 2, 2, 75, "manual"),
        ("鮪魚飯糰", 210, 6, 38, 4, 2, 1, 480, "manual"),
        ("香蕉", 105, 1.3, 27, 0.4, 14, 3, 1, "manual"),
        ("地瓜", 180, 2, 41, 0.2, 12, 5, 55, "manual"),
        ("牛肉麵", 750, 32, 90, 26, 5, 4, 1800, "manual"),
        ("大杯半糖珍珠奶茶", 480, 7, 82, 14, 38, 0, 120, "manual"),
        ("早餐店蛋餅加無糖豆漿", 480, 22, 46, 23, 4, 4, 725, "manual"),
        ("便利商店鮪魚飯糰加茶葉蛋", 288, 12, 39, 9, 2, 1, 780, "manual"),
        ("牛肉麵一碗湯喝一半", 680, 30, 88, 22, 5, 4, 1300, "manual"),
    ]
    cursor.executemany("""
        INSERT OR IGNORE INTO foods
        (name, calories, protein, carbs, fat, sugar, fiber, sodium, source, input_key, estimate_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [(name, cal, pro, carb, fat, sugar, fiber, sodium, source, normalize_food_input(name), "")
           for name, cal, pro, carb, fat, sugar, fiber, sodium, source in sample_foods])

    # 舊資料若沒有 input_key，補上，否則資料庫優先查詢會找不到。
    rows = cursor.execute("SELECT id, name, input_key FROM foods").fetchall()
    for row_id, name, input_key in rows:
        if not input_key:
            new_key = normalize_food_input(name.split("（")[0])
            try:
                cursor.execute("UPDATE foods SET input_key = ? WHERE id = ?", (new_key, row_id))
            except sqlite3.IntegrityError:
                pass

    seed_demo_user(cursor, "free_demo", "free123", "free", "維持健康", 1800, 70)
    seed_demo_user(cursor, "premium_demo", "premium123", "premium", "增肌", 2200, 110)

    db.commit()
    db.close()


def login_required(view):
    @wraps(view)
    def wrapped_view(**kwargs):
        if session.get("user_id") is None:
            return redirect(url_for("login"))
        return view(**kwargs)
    return wrapped_view


@app.before_request
def load_logged_in_user() -> None:
    user_id = session.get("user_id")
    g.user = None if user_id is None else get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    g.is_premium = is_premium_user(g.user)
    g.plan_name = get_plan_name(g.user["plan"] if g.user else None)


@app.context_processor
def inject_helpers():
    return {"plan_name": get_plan_name}


def safe_float(value: Any, default: float = 0) -> float:
    try:
        return max(float(value), 0)
    except (TypeError, ValueError):
        return default


def find_matching_food(food_description: str) -> sqlite3.Row | None:
    """資料庫優先：先找完全相同，再找食物名稱包含關係。找不到才允許 premium 呼叫 GPT。"""
    input_key = normalize_food_input(food_description)
    if not input_key:
        return None

    db = get_db()
    row = db.execute("SELECT * FROM foods WHERE input_key = ? LIMIT 1", (input_key,)).fetchone()
    if row:
        return row

    foods = db.execute("SELECT * FROM foods ORDER BY LENGTH(name) DESC").fetchall()
    for food in foods:
        food_name = str(food["name"] or "").split("（")[0]
        food_key = normalize_food_input(food_name)
        if not food_key:
            continue
        if input_key == food_key or food_key in input_key or input_key in food_key:
            if not food["input_key"]:
                try:
                    db.execute("UPDATE foods SET input_key = ? WHERE id = ?", (food_key, food["id"]))
                    db.commit()
                except sqlite3.IntegrityError:
                    pass
            return food
    return None


def fallback_estimate(food_description: str) -> dict[str, Any]:
    text = food_description.lower()
    presets = [
        ("珍珠", {"calories": 600, "protein": 8, "carbs": 95, "fat": 18, "sugar": 55, "fiber": 0, "sodium": 120}),
        ("雞腿", {"calories": 780, "protein": 35, "carbs": 95, "fat": 28, "sugar": 10, "fiber": 5, "sodium": 1200}),
        ("便當", {"calories": 700, "protein": 30, "carbs": 90, "fat": 24, "sugar": 8, "fiber": 4, "sodium": 1100}),
        ("沙拉", {"calories": 220, "protein": 8, "carbs": 20, "fat": 10, "sugar": 6, "fiber": 6, "sodium": 350}),
        ("蛋餅", {"calories": 350, "protein": 12, "carbs": 38, "fat": 17, "sugar": 2, "fiber": 2, "sodium": 650}),
        ("牛肉麵", {"calories": 750, "protein": 32, "carbs": 90, "fat": 26, "sugar": 5, "fiber": 4, "sodium": 1800}),
        ("飯糰", {"calories": 210, "protein": 6, "carbs": 38, "fat": 4, "sugar": 2, "fiber": 1, "sodium": 480}),
    ]
    values = {"calories": 450, "protein": 18, "carbs": 55, "fat": 16, "sugar": 8, "fiber": 3, "sodium": 700}
    for keyword, preset in presets:
        if keyword in text:
            values = preset
            break
    return {
        "food_name": food_description.strip()[:60] or "自訂餐點",
        "serving_description": "依使用者輸入估算的一份",
        **values,
        "confidence": "low",
        "notes": "未設定 OPENAI_API_KEY 或 API 呼叫失敗，使用本機示範估算值。",
    }


def estimate_food_with_gpt(food_description: str) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    if not api_key:
        return fallback_estimate(food_description)

    prompt = f"""
請根據以下使用者輸入，估算整份餐點的營養資訊。請以台灣常見食物份量與外食情境估算。
使用者輸入：{food_description}
規則：
1. 若份量不明，請以一般成人常見一份估算。
2. 數值必須是整份餐點總量，不是每100g。
3. 這是健康管理用途估算，不是醫療診斷。
4. notes 用繁體中文簡短說明估算依據。
"""
    try:
        completion = OpenAI(api_key=api_key).chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "你是營養估算助手，專門把自然語言餐點描述轉成結構化營養資料。"},
                {"role": "user", "content": prompt},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "nutrition_estimate", "schema": NUTRITION_SCHEMA, "strict": True},
            },
        )
        data = json.loads(completion.choices[0].message.content)
    except Exception as exc:
        data = fallback_estimate(food_description)
        data["notes"] = f"API 呼叫失敗，已改用本機示範估算。原因：{exc}"

    for key in ["calories", "protein", "carbs", "fat", "sugar", "fiber", "sodium"]:
        data[key] = safe_float(data.get(key, 0))
    data["food_name"] = str(data.get("food_name") or food_description).strip()[:80]
    data["serving_description"] = str(data.get("serving_description") or "一份").strip()[:80]
    if data.get("confidence") not in {"low", "medium", "high"}:
        data["confidence"] = "medium"
    data["notes"] = str(data.get("notes") or "").strip()[:300]
    return data


def save_estimated_food(estimate: dict[str, Any], input_key: str | None = None) -> int:
    db = get_db()
    display_name = f"{estimate['food_name']}（{estimate['serving_description']}）"
    db.execute("""
        INSERT OR IGNORE INTO foods
        (name, calories, protein, carbs, fat, sugar, fiber, sodium, source, input_key, estimate_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        display_name,
        estimate["calories"], estimate["protein"], estimate["carbs"], estimate["fat"],
        estimate["sugar"], estimate["fiber"], estimate["sodium"],
        "gpt" if os.getenv("OPENAI_API_KEY") else "fallback",
        input_key,
        json.dumps(estimate, ensure_ascii=False),
    ))
    db.commit()
    row = None
    if input_key:
        row = db.execute("SELECT id FROM foods WHERE input_key = ?", (input_key,)).fetchone()
    if row is None:
        row = db.execute("SELECT id FROM foods WHERE name = ?", (display_name,)).fetchone()
    return int(row["id"])


def summarize_logs(user_id: int, target_date: str) -> dict:
    rows = get_db().execute("""
        SELECT fl.*, f.name, f.calories, f.protein, f.carbs, f.fat, f.sugar, f.fiber, f.sodium, f.source, f.estimate_json
        FROM food_logs fl JOIN foods f ON fl.food_id = f.id
        WHERE fl.user_id = ? AND fl.log_date = ?
        ORDER BY fl.created_at DESC
    """, (user_id, target_date)).fetchall()
    totals = {"calories": 0, "protein": 0, "carbs": 0, "fat": 0, "sugar": 0, "fiber": 0, "sodium": 0}
    for row in rows:
        qty = row["quantity"]
        for key in totals:
            totals[key] += float(row[key]) * qty
    return {"rows": rows, "totals": totals}


def generate_advice(totals: dict, user: sqlite3.Row) -> list[str]:
    advice = []
    goal = user["goal"]

    # 基本熱量判斷
    if totals["calories"] > user["daily_calorie_goal"]:
        advice.append("今日熱量已超過目標，建議下一餐選擇低油、低糖、較清淡的餐點。")
    elif totals["calories"] < user["daily_calorie_goal"] * 0.65:
        advice.append("目前熱量攝取偏低，若還有活動量，可適度補充均衡正餐。")
    else:
        advice.append("今日熱量接近目標範圍，整體控制良好。")

    # 根據健康目標給不同重點
    if goal == "減重":
        if totals["calories"] > user["daily_calorie_goal"]:
            advice.append("減重目標下，今日熱量偏高，建議減少油炸食物、含糖飲料與宵夜。")
        if totals["sugar"] > 50:
            advice.append("糖分偏高可能影響減重效果，建議將手搖飲改成無糖或微糖。")
        advice.append("減重期間建議優先選擇高蛋白、低油脂、足量蔬菜的餐點。")

    elif goal == "增肌":
        if totals["protein"] < user["protein_goal"]:
            advice.append("增肌目標下，蛋白質攝取不足，建議補充雞胸肉、雞蛋、豆腐、魚類或無糖豆漿。")
        if totals["calories"] < user["daily_calorie_goal"]:
            advice.append("增肌需要足夠熱量，目前熱量尚未達標，可增加主食與蛋白質來源。")
        advice.append("增肌期間建議每餐都安排蛋白質來源，並搭配規律重量訓練。")

    elif goal == "控制血糖":
        if totals["sugar"] > 50:
            advice.append("控制血糖目標下，今日糖分偏高，建議減少甜點、含糖飲料與精緻澱粉。")
        if totals["carbs"] > 250:
            advice.append("碳水攝取較高，建議選擇全穀類、地瓜、糙米等較穩定的澱粉來源。")
        advice.append("控制血糖時，建議每餐搭配蛋白質與蔬菜，避免單吃大量澱粉。")

    elif goal == "控制血壓":
        if totals["sodium"] > user["sodium_limit"]:
            advice.append("控制血壓目標下，鈉含量偏高，建議減少湯品、醬料、加工食品與重口味外食。")
        advice.append("控制血壓時，建議選擇清蒸、水煮、少醬料餐點，並增加蔬菜攝取。")

    else:  # 維持健康
        if totals["fiber"] < 20:
            advice.append("膳食纖維偏低，建議增加蔬菜、水果、全穀類或地瓜。")
        advice.append("維持健康目標下，建議保持熱量穩定、營養均衡與規律飲食。")

    # Premium 才顯示更細的營養提醒
    if is_premium_user(user):
        if totals["protein"] < user["protein_goal"]:
            advice.append("蛋白質攝取低於目標，可增加雞蛋、豆腐、無糖豆漿或雞胸肉。")
        if totals["sodium"] > user["sodium_limit"]:
            advice.append("鈉含量偏高，建議減少湯品、加工食品與重口味外食。")
        if totals["sugar"] > 50:
            advice.append("糖分偏高，建議將含糖飲料改成無糖或微糖。")
        if totals["fiber"] < 20:
            advice.append("膳食纖維偏低，建議增加蔬菜、水果或全穀類。")
    else:
        advice.append("免費版提供基本熱量與三大營養素摘要；升級 Premium 可查看糖分、纖維、鈉含量與更完整建議。")

    return advice

def fallback_ai_analysis(totals: dict, user: sqlite3.Row) -> dict[str, Any]:
    """沒有 API Key 或 API 失敗時的示範 AI 分析。"""
    risk_points = []
    suggestions = []

    if totals["calories"] > user["daily_calorie_goal"]:
        risk_points.append("今日熱量已超過目標，可能影響體重控制。")
        suggestions.append("下一餐建議選擇低油、低糖、增加蔬菜的餐點。")
    else:
        risk_points.append("今日熱量尚未明顯超標，但仍需注意餐點品質。")
        suggestions.append("可以維持目前熱量控制，並注意蛋白質與纖維攝取。")

    if totals["protein"] < user["protein_goal"]:
        risk_points.append("蛋白質攝取低於目標，可能影響飽足感與肌肉維持。")
        suggestions.append("可補充雞蛋、豆腐、無糖豆漿、雞胸肉或魚類。")

    if totals["sodium"] > user["sodium_limit"]:
        risk_points.append("鈉含量偏高，可能來自湯品、加工食品或重口味外食。")
        suggestions.append("建議減少喝湯、醬料與加工食品。")

    if totals["fiber"] < 20:
        risk_points.append("膳食纖維偏低，蔬菜、水果或全穀類可能不足。")
        suggestions.append("下一餐可增加青菜、地瓜、燕麥或水果。")

    return {
        "overview": "這是系統根據今日飲食紀錄產生的示範分析。若設定 OPENAI_API_KEY，會改由 GPT 產生更完整的專屬分析。",
        "positive_points": [
            "已建立飲食紀錄，有助於追蹤長期健康狀況。",
            "系統已整理今日熱量與營養攝取，方便進一步調整飲食。",
        ],
        "risk_points": risk_points[:4],
        "suggestions": suggestions[:4],
        "next_meal_suggestion": "下一餐建議選擇清淡主食、足量蛋白質與至少一份蔬菜。",
    }


def generate_ai_analysis_with_gpt(totals: dict, user: sqlite3.Row, logs: list[sqlite3.Row], target_date: str) -> dict[str, Any]:
    """Premium 專屬：根據當日飲食紀錄呼叫 GPT 產生個人化 AI 分析。"""
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    if not api_key:
        return fallback_ai_analysis(totals, user)

    food_items = []
    for row in logs:
        food_items.append({
            "meal_type": row["meal_type"],
            "original_input": row["original_input"],
            "food_name": row["name"],
            "quantity": row["quantity"],
            "calories": round(float(row["calories"]) * float(row["quantity"]), 1),
            "protein": round(float(row["protein"]) * float(row["quantity"]), 1),
            "carbs": round(float(row["carbs"]) * float(row["quantity"]), 1),
            "fat": round(float(row["fat"]) * float(row["quantity"]), 1),
            "sugar": round(float(row["sugar"]) * float(row["quantity"]), 1),
            "fiber": round(float(row["fiber"]) * float(row["quantity"]), 1),
            "sodium": round(float(row["sodium"]) * float(row["quantity"]), 1),
        })

    prompt = f"""
請根據以下使用者當日飲食紀錄，產生一份繁體中文的專屬 AI 飲食分析。

日期：{target_date}

使用者目標：
- 健康目標：{user["goal"]}
- 每日熱量目標：{user["daily_calorie_goal"]} kcal
- 每日蛋白質目標：{user["protein_goal"]} g
- 每日鈉含量上限：{user["sodium_limit"]} mg

今日營養總量：
- 熱量：{round(totals["calories"], 1)} kcal
- 蛋白質：{round(totals["protein"], 1)} g
- 碳水：{round(totals["carbs"], 1)} g
- 脂肪：{round(totals["fat"], 1)} g
- 糖分：{round(totals["sugar"], 1)} g
- 膳食纖維：{round(totals["fiber"], 1)} g
- 鈉：{round(totals["sodium"], 1)} mg

今日飲食紀錄：
{json.dumps(food_items, ensure_ascii=False, indent=2)}

請輸出：
1. overview：整體飲食摘要
2. positive_points：做得好的地方，2 到 3 點
3. risk_points：需要注意的地方，2 到 4 點
4. suggestions：具體改善建議，3 到 5 點
5. next_meal_suggestion：下一餐建議

注意：
- 不要做醫療診斷
- 不要說自己是醫師
- 建議要具體、貼近日常外食情境
- 使用繁體中文
"""

    try:
        completion = OpenAI(api_key=api_key).chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "你是健康飲食 SaaS 平台中的 AI 飲食分析助手，負責根據飲食紀錄提供生活化、非醫療性的飲食建議。",
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "ai_nutrition_analysis",
                    "schema": AI_ANALYSIS_SCHEMA,
                    "strict": True,
                },
            },
        )

        data = json.loads(completion.choices[0].message.content)

    except Exception as exc:
        data = fallback_ai_analysis(totals, user)
        data["overview"] = f"GPT 分析暫時失敗，已改用本機示範分析。原因：{exc}"

    return data

@app.route("/")
def index():
    if g.user:
        return redirect(url_for("dashboard"))
    return render_template("index.html")


@app.route("/register", methods=("GET", "POST"))
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"].strip()
        daily_calorie_goal = int(request.form.get("daily_calorie_goal", 1800))
        protein_goal = int(request.form.get("protein_goal", 70))
        goal = request.form.get("goal", "維持健康")
        error = None
        if not username:
            error = "請輸入帳號。"
        elif not password:
            error = "請輸入密碼。"
        if error is None:
            try:
                db = get_db()
                # 使用者自行註冊預設為免費版，付費版用 premium_demo 展示。
                db.execute(
                    "INSERT INTO users (username, password_hash, plan, daily_calorie_goal, protein_goal, goal) VALUES (?, ?, ?, ?, ?, ?)",
                    (username, generate_password_hash(password), "free", daily_calorie_goal, protein_goal, goal),
                )
                db.commit()
                flash("註冊成功，請登入。", "success")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                error = "這個帳號已經被使用。"
        flash(error, "error")
    return render_template("register.html")


@app.route("/login", methods=("GET", "POST"))
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"].strip()
        user = get_db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user is None or not check_password_hash(user["password_hash"], password):
            flash("帳號或密碼錯誤。", "error")
        else:
            session.clear()
            session["user_id"] = user["id"]
            return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


def build_dashboard_context(target_date: str, ai_analysis: dict[str, Any] | None = None) -> dict[str, Any]:
    summary = summarize_logs(g.user["id"], target_date)
    totals = summary["totals"]
    advice = generate_advice(totals, g.user)

    week_labels, week_calories = [], []
    selected_day = datetime.strptime(target_date, "%Y-%m-%d").date()

    for i in range(6, -1, -1):
        day = selected_day - timedelta(days=i)
        week_labels.append(day.strftime("%m/%d"))
        week_calories.append(
            round(summarize_logs(g.user["id"], day.isoformat())["totals"]["calories"], 1)
        )

    calorie_goal = g.user["daily_calorie_goal"]
    remaining = max(calorie_goal - totals["calories"], 0)
    calorie_percent = min(round(totals["calories"] / calorie_goal * 100, 1), 100) if calorie_goal else 0

    return {
        "target_date": target_date,
        "logs": summary["rows"],
        "totals": totals,
        "advice": advice,
        "remaining": remaining,
        "calorie_percent": calorie_percent,
        "week_labels": week_labels,
        "week_calories": week_calories,
        "is_premium": g.is_premium,
        "ai_analysis": ai_analysis,
    }


@app.route("/dashboard")
@login_required
def dashboard():
    target_date = request.args.get("date", date.today().isoformat())
    return render_template("dashboard.html", **build_dashboard_context(target_date))

@app.route("/ai-analysis", methods=("POST",))
@login_required
def ai_analysis():
    if not g.is_premium:
        flash("專屬 AI 分析是 Premium 會員功能。", "error")
        return redirect(url_for("plans"))

    target_date = request.form.get("target_date") or date.today().isoformat()
    summary = summarize_logs(g.user["id"], target_date)

    if not summary["rows"]:
        flash("這一天還沒有飲食紀錄，請先新增飲食紀錄後再產生 AI 分析。", "error")
        return redirect(url_for("dashboard", date=target_date))

    analysis = generate_ai_analysis_with_gpt(
        summary["totals"],
        g.user,
        summary["rows"],
        target_date,
    )

    flash("已產生專屬 AI 分析。", "success")
    return render_template(
        "dashboard.html",
        **build_dashboard_context(target_date, ai_analysis=analysis),
    )

@app.route("/logs/add", methods=("GET", "POST"))
@login_required
def add_log():
    if request.method == "POST":
        food_description = request.form["food_description"].strip()
        meal_type = request.form["meal_type"]
        quantity = float(request.form.get("quantity", 1))
        log_date = request.form.get("log_date") or date.today().isoformat()
        if not food_description:
            flash("請輸入食物名稱或餐點描述。", "error")
            return redirect(url_for("add_log"))

        input_key = normalize_food_input(food_description)
        cached_food = find_matching_food(food_description)

        if cached_food:
            food_id = cached_food["id"]
            note = "已先檢查資料庫並使用既有食物資料，未呼叫 GPT API。"
        else:
            if not g.is_premium:
                flash("資料庫找不到這個食物。免費版不使用 GPT 新增估算食物，請改輸入資料庫已有食物，或使用 Premium 帳號展示 GPT 估算。", "error")
                return redirect(url_for("add_log"))
            estimate = estimate_food_with_gpt(food_description)
            food_id = save_estimated_food(estimate, input_key)
            note = f"資料庫查無對應食物，Premium 已透過 GPT 估算。信心度：{estimate['confidence']}｜{estimate['notes']}"

        get_db().execute("""
            INSERT INTO food_logs (user_id, food_id, meal_type, quantity, log_date, original_input, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (g.user["id"], food_id, meal_type, quantity, log_date, food_description, note))
        get_db().commit()
        flash("已新增飲食紀錄。", "success")
        return redirect(url_for("dashboard", date=log_date))

    examples = ["雞腿便當", "一個雞腿便當，飯半碗", "大杯半糖珍珠奶茶", "早餐店蛋餅加無糖豆漿", "便利商店鮪魚飯糰加茶葉蛋", "牛肉麵一碗，湯喝一半"]
    return render_template("add_log.html", today=date.today().isoformat(), examples=examples, is_premium=g.is_premium)


@app.route("/logs/<int:log_id>/delete", methods=("POST",))
@login_required
def delete_log(log_id: int):
    get_db().execute("DELETE FROM food_logs WHERE id = ? AND user_id = ?", (log_id, g.user["id"]))
    get_db().commit()
    flash("已刪除紀錄。", "success")
    return redirect(url_for("dashboard"))


@app.route("/foods", methods=("GET", "POST"))
@login_required
def foods():
    if request.method == "POST":
        name = request.form["name"].strip()
        values = (
            name,
            float(request.form.get("calories", 0)),
            float(request.form.get("protein", 0)),
            float(request.form.get("carbs", 0)),
            float(request.form.get("fat", 0)),
            float(request.form.get("sugar", 0)),
            float(request.form.get("fiber", 0)),
            float(request.form.get("sodium", 0)),
            "manual",
            normalize_food_input(name),
            "",
        )
        try:
            get_db().execute("""
                INSERT INTO foods (name, calories, protein, carbs, fat, sugar, fiber, sodium, source, input_key, estimate_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, values)
            get_db().commit()
            flash("已新增食物資料。", "success")
        except sqlite3.IntegrityError:
            flash("食物名稱或查詢鍵已存在。", "error")
        return redirect(url_for("foods"))
    all_foods = get_db().execute("SELECT * FROM foods ORDER BY id DESC").fetchall()
    return render_template("foods.html", foods=all_foods)


@app.route("/plans")
def plans():
    return render_template("plans.html")


@app.cli.command("init-db")
def init_db_command() -> None:
    init_db()
    print("Initialized the database.")

@app.route("/admin/make-premium/<username>")
def make_premium(username: str):
    db = get_db()
    db.execute("UPDATE users SET plan = ? WHERE username = ?", ("premium", username))
    db.commit()
    flash(f"{username} 已切換為 Premium。", "success")
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
