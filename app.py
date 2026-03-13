# -*- coding: utf-8 -*-
"""
Умный конвертер валют — веб-сервис с курсами ЦБ РФ, историей и графиком.
Облачное приложение для курса «Облачные технологии».
"""
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import boto3
from botocore.client import Config
from flask import Flask, render_template, request, jsonify
from werkzeug.utils import secure_filename
import requests

app = Flask(__name__)
DB_PATH = os.environ.get("DB_PATH", "converter.db")

S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "https://storage.yandexcloud.net")
S3_REGION = os.environ.get("S3_REGION", "ru-central1")
S3_BUCKET = os.environ.get("S3_BUCKET")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY")

# Валюты ЦБ РФ (код и название)
CURRENCIES = {
    "RUB": "Российский рубль",
    "USD": "Доллар США",
    "EUR": "Евро",
    "CNY": "Китайский юань",
    "GBP": "Британский фунт",
    "JPY": "Японская иена",
    "CHF": "Швейцарский франк",
    "TRY": "Турецкая лира",
    "KZT": "Казахстанский тенге",
}

CBR_DAILY = "https://www.cbr-xml-daily.ru/daily_json.js"
CBR_ARCHIVE = "https://www.cbr-xml-daily.ru/archive/{year}/{month:02d}/{day:02d}/daily_json.js"


def get_s3_client():
    """Вернуть S3‑клиент для Object Storage Яндекса или None, если не настроен."""
    if not (S3_BUCKET and S3_ACCESS_KEY and S3_SECRET_KEY):
        return None
    session = boto3.session.Session()
    return session.client(
        service_name="s3",
        region_name=S3_REGION,
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        config=Config(s3={"addressing_style": "virtual"}),
    )


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            amount REAL NOT NULL,
            from_cur TEXT NOT NULL,
            to_cur TEXT NOT NULL,
            rate REAL NOT NULL,
            result REAL NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


def fetch_cbr_rates(date=None):
    """Получить курсы ЦБ РФ на дату (date=None — актуальный курс с основного URL)."""
    if date:
        url = CBR_ARCHIVE.format(year=date.year, month=date.month, day=date.day)
    else:
        url = CBR_DAILY
    try:
        r = requests.get(url, timeout=5)
        r.raise_for_status()
        data = r.json()
        # ЦБ даёт курсы к рублю: Valute.USD.Value = сколько рублей за 1 USD
        rates = {"RUB": 1.0}
        for code, v in data.get("Valute", {}).items():
            rates[code] = float(v["Value"]) / float(v.get("Nominal", 1))
        return rates
    except requests.HTTPError as e:
        # 404 нормально для архива: выходные, праздники или сегодняшний день ещё не выложен
        if e.response is not None and e.response.status_code == 404:
            app.logger.debug("CBR archive not found %s", url)
        else:
            app.logger.warning("CBR fetch failed %s: %s", url, e)
        return None
    except Exception as e:
        app.logger.warning("CBR fetch failed %s: %s", url, e)
        return None


def convert_amount(amount, from_cur, to_cur, rates):
    """Конвертировать сумму из from_cur в to_cur по словарю rates (RUB за 1 ед. валюты)."""
    if from_cur not in rates or to_cur not in rates:
        return None
    # 1 from_cur = rates[from_cur] RUB; 1 to_cur = rates[to_cur] RUB
    # amount from_cur = amount * (rates[from_cur] / rates[to_cur]) to_cur
    rate = rates[from_cur] / rates[to_cur]
    result = amount * rate
    return result, rate


def save_to_history(amount, from_cur, to_cur, rate, result):
    conn = get_db()
    conn.execute(
        "INSERT INTO history (amount, from_cur, to_cur, rate, result) VALUES (?, ?, ?, ?, ?)",
        (amount, from_cur, to_cur, rate, result),
    )
    conn.commit()
    conn.close()


def get_history_list(limit=20):
    conn = get_db()
    rows = conn.execute(
        "SELECT amount, from_cur, to_cur, rate, result, created_at FROM history ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_trend(from_cur, to_cur, days=7):
    """Вернуть (points, trend, percent): список курсов, 'up'/'down'/'flat' и изменение в %."""
    points = []
    today = datetime.now(timezone.utc).date()
    for i in range(days):
        d = today - timedelta(days=i)
        # Для сегодня используем основной URL (архив на текущий день может ещё не быть)
        rates = fetch_cbr_rates(None if d == today else d)
        if not rates:
            continue
        res = convert_amount(1, from_cur, to_cur, rates)
        if res:
            _, rate = res
            points.append({"date": d.isoformat(), "rate": round(rate, 6)})
    points.reverse()
    if len(points) < 2:
        return points, "flat", 0.0
    first = points[0]["rate"]
    last = points[-1]["rate"]
    if first == 0:
        return points, "flat", 0.0
    percent = (last - first) / first * 100
    if last > first:
        trend = "up"
    elif last < first:
        trend = "down"
    else:
        trend = "flat"
    return points, trend, round(percent, 1)


@app.route("/")
def index():
    return render_template(
        "index.html",
        currencies=CURRENCIES,
        history=get_history_list(),
    )


@app.route("/convert", methods=["POST"])
def convert():
    try:
        amount = float(request.form.get("amount", 1))
        from_cur = request.form.get("from_cur", "RUB").strip().upper()
        to_cur = request.form.get("to_cur", "USD").strip().upper()
        days = int(request.form.get("days", 7))
    except (ValueError, TypeError):
        return jsonify({"error": "Неверные параметры"}), 400

    if from_cur not in CURRENCIES or to_cur not in CURRENCIES:
        return jsonify({"error": "Неизвестная валюта"}), 400
    if from_cur == to_cur:
        return jsonify({"error": "Выберите разные валюты"}), 400

    rates = fetch_cbr_rates()
    if not rates:
        return jsonify({"error": "Не удалось получить курсы ЦБ РФ"}), 502

    res = convert_amount(amount, from_cur, to_cur, rates)
    if not res:
        return jsonify({"error": "Ошибка конвертации"}), 400

    result, rate = res
    save_to_history(amount, from_cur, to_cur, rate, result)

    # Возвращаем новую запись для обновления блока истории без перезагрузки
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    history_row = {
        "amount": amount,
        "from_cur": from_cur,
        "to_cur": to_cur,
        "rate": round(rate, 6),
        "result": round(result, 2),
        "created_at": now,
    }

    chart_data, trend, trend_percent = get_trend(from_cur, to_cur, days=days)
    if trend == "up":
        trend_text = f"поднялся на {trend_percent}%"
    elif trend == "down":
        trend_text = f"упал на {abs(trend_percent)}%"
    else:
        trend_text = "без изменений"

    return jsonify({
        "amount": amount,
        "from_cur": from_cur,
        "to_cur": to_cur,
        "rate": round(rate, 6),
        "result": round(result, 2),
        "trend": trend,
        "trend_text": trend_text,
        "chart": chart_data,
        "history_row": history_row,
    })


@app.route("/api/rates")
def api_rates():
    """JSON с текущими курсами (для внешнего использования)."""
    rates = fetch_cbr_rates()
    if not rates:
        return jsonify({"error": "CBR unavailable"}), 502
    return jsonify(rates)


@app.route("/background", methods=["POST"])
def background():
    """Загрузить фон в Object Storage и вернуть публичный URL и blur."""
    file = request.files.get("image")
    if not file or not file.filename:
        return jsonify({"error": "Файл не выбран"}), 400

    try:
        blur = int(request.form.get("blur", 6))
        if blur < 0 or blur > 40:
            blur = 6
    except (TypeError, ValueError):
        blur = 6

    s3 = get_s3_client()
    if not s3:
        return jsonify({"error": "Object Storage не настроен на сервере"}), 500

    filename = secure_filename(file.filename)
    ext = (filename.rsplit(".", 1)[-1].lower() if "." in filename else "jpg")
    if ext not in {"jpg", "jpeg", "png", "webp"}:
        ext = "jpg"

    key = f"backgrounds/{uuid4().hex}.{ext}"

    try:
        s3.upload_fileobj(
            file,
            S3_BUCKET,
            key,
            ExtraArgs={
                "ContentType": file.mimetype or "image/jpeg",
                "ACL": "public-read",
            },
        )
    except Exception as e:
        app.logger.warning("S3 upload failed: %s", e)
        return jsonify({"error": "Не удалось загрузить файл в Object Storage"}), 502

    url = f"{S3_ENDPOINT.rstrip('/')}/{S3_BUCKET}/{key}"
    return jsonify({"url": url, "blur": blur})


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG", "0") == "1")
