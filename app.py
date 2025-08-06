# -*- coding: utf-8 -*-
import os
import re
import time
import json
import functools
import fitz                 # PyMuPDF
import requests
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from bs4 import BeautifulSoup

app = Flask(__name__)
CORS(app)

# ----------------------------------------------------------------------
# Константы
# ----------------------------------------------------------------------
PDF_IFRAME_URL = "https://new.fitorf.ru/validate/ks/{}/print"
CERBERUS_BASE_URL = "https://cerberus.vetrf.ru/cerberus/certified/exporter/pub"

KEYWORDS_DEFAULT = ["Эгилопс"]          # Можно менять в UI
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# ----------------------------------------------------------------------
# Утилиты
# ----------------------------------------------------------------------
def extract_issue_date(text: str) -> str | None:
    """Ищет в тексте дату выдачи сертификата (dd.mm.YYYY)."""
    patterns = [
        r"(?:выдан[а-я]*|дата выдачи|оформлен[а-я]*)[^0-9]{0,20}(\d{2}\.\d{2}\.\d{4})",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            return m.group(1)

    dates = re.findall(r"\d{2}\.\d{2}\.\d{4}", text)
    return dates[-1] if dates else None


def fetch_pdf_text(cert_number: str) -> tuple[str | None, str | None]:
    """Скачивает PDF‑сертификат и возвращает полностью его текст."""
    try:
        url = PDF_IFRAME_URL.format(cert_number)
        resp = SESSION.get(url, timeout=15)
        if resp.status_code != 200:
            return None, f"PDF не найден (status={resp.status_code})"

        tmp_path = f"tmp_{cert_number}.pdf"
        with open(tmp_path, "wb") as f:
            f.write(resp.content)

        with fitz.open(tmp_path) as doc:
            text = "\n".join(page.get_text() for page in doc)

        os.remove(tmp_path)
        return text, None
    except Exception as exc:
        return None, f"Ошибка при загрузке PDF: {exc}"


# ----------------------------------------------------------------------
# Работа с Cerberus (по ИНН и/или по названию)
# ----------------------------------------------------------------------
def _parse_cerberus_table_row(row) -> dict | None:
    """Парсит одну строку <tr> таблицы Cerberus в словарь."""
    cols = [c.get_text(strip=True) for c in row.find_all("td")]
    if not cols:
        return None

    # Попытка «по‑умолчанию» – ИНН в колонке 2, страна в колонке 4, статус в колонке 5
    try:
        inn = cols[2]
        destination = cols[4]
        status = cols[5]
        return {"inn": inn, "destination": destination, "status": status}
    except Exception:
        # Фолбэк‑поиск – ищем ИНН где‑нибудь, потом страну и статус
        inn = destination = status = None
        for c in cols:
            if re.fullmatch(r"\d{10,12}", c):
                inn = c
            if "кит" in c.lower():
                destination = c
            if any(w in c.lower() for w in ["действ", "прекр", "отмен"]):
                status = c
        if inn:
            return {"inn": inn, "destination": destination, "status": status}
    return None


@functools.lru_cache(maxsize=500)
def _cerberus_get(url: str) -> requests.Response:
    """Кеширует GET‑запросы к Cerberus (чтобы не перегружать сервис)."""
    return SESSION.get(url, timeout=10)


def _search_cerberus(params: dict) -> tuple[dict | None, str | None]:
    """
    Делает запрос к Cerberus с произвольными GET‑параметрами.
    Возвращает найденный словарь {inn, destination, status} либо (None, err_msg).
    """
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{CERBERUS_BASE_URL}?{query}"
    resp = _cerberus_get(url)

    if resp.status_code != 200:
        return None, f"Cerberus недоступен (status={resp.status_code})"

    soup = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table")
    if not table:
        # Если таблицы нет, проверим, не «нет записей» ли это
        page_text = soup.get_text(separator=" ").lower()
        if any(word in page_text for word in ["не найдено", "записей не найдено", "ничего не найдено"]):
            return None, None          # запись действительно отсутствует
        return None, "Таблица не найдена в ответе GET"

    rows = table.find_all("tr")
    for tr in rows[1:]:
        parsed = _parse_cerberus_table_row(tr)
        if parsed:
            # При поиске по названию может быть несколько ИНН – берём каждую
            return parsed, None

    return None, None  # запись не найдена, но ошибка нет


def search_cerberus_by_inn(inn: str) -> tuple[dict | None, str | None]:
    """Поиск только по ИНН (основной путь)."""
    return _search_cerberus({"inn": inn})


def search_cerberus_by_name(name: str) -> tuple[dict | None, str | None]:
    """
    Поиск по названию компании.
    Сервис Cerberus поддерживает параметр `searchValue` и `searchBy=name`.
    Если найдено, возвращаем первую подходящую строку.
    """
    # 1) Формируем GET‑параметры, которые имитируют работу формы
    params = {"searchValue": name, "searchBy": "name"}
    # Cerberus иногда отвечает POST‑ом, но GET обычно работает.
    return _search_cerberus(params)


# ----------------------------------------------------------------------
# Логика анализа сертификата
# ----------------------------------------------------------------------
def analyze_certificate(cert_number: str, keywords: list[str]) -> dict:
    """
    Основная бизнес‑логика:
    1) Скачиваем PDF → получаем текст.
    2) Ищем ключевые слова.
    3) Дату выдачи → статус «действующий/просрочен».
    4) ИНН → Cerberus.
    5) При отсутствии записи по ИНН – делаем поисковый запрос по названию.
    6) Формируем итоговый словарь, совместимый с фронтендом.
    """
    result = {
        "number": cert_number,
        "found": False,
        "info": "PDF не найден",
        "destination": "-",
        "enterprise_status": "-",
        "validity": "-",
        "date_raw": "-",
    }

    # -------------------------------------------------------------
    # 1. PDF
    # -------------------------------------------------------------
    pdf_text, pdf_err = fetch_pdf_text(cert_number)
    if pdf_err:
        result["info"] = pdf_err
        return result

    # -------------------------------------------------------------
    # 2. Ключевые слова
    # -------------------------------------------------------------
    found_keywords = [kw for kw in keywords if re.search(re.escape(kw), pdf_text, re.IGNORECASE)]
    result["found"] = bool(found_keywords)

    # -------------------------------------------------------------
    # 3. Дата выдачи
    # -------------------------------------------------------------
    raw_date = extract_issue_date(pdf_text)
    result["date_raw"] = raw_date or "-"
    if raw_date:
        try:
            dt = datetime.strptime(raw_date, "%d.%m.%Y")
            if dt < datetime.now() - timedelta(days=30):
                result["validity"] = f"Просрочен (выдан {raw_date})"
            else:
                result["validity"] = f"Действующий (выдан {raw_date})"
        except Exception:
            result["validity"] = f"Не удалось разобрать дату ({raw_date})"
    else:
        result["validity"] = "Дата выдачи не найдена"

    # -------------------------------------------------------------
    # 4. ИНН → Cerberus
    # -------------------------------------------------------------
    inn_match = re.search(r"ИНН\s*:?\s*(\d{10,12})", pdf_text, re.IGNORECASE)
    if not inn_match:
        # ИНН в PDF не найден – выводим то, что уже успели собрать
        result["info"] = (
            f"{'Ключевые слова: ' + ', '.join(found_keywords) if found_keywords else 'Ключевые слова не найдены'}\n"
            f"{result['validity']}"
        )
        return result

    inn = inn_match.group(1)

    # Пытаемся найти запись по ИНН
    cerberus_data, cer_err = search_cerberus_by_inn(inn)

    if cer_err:
        # Ошибка обращения к Cerberus (сетевая, 5xx и т.п.)
        result["info"] = f"ИНН найден ({inn}), но запрос к Cerberus завершился ошибкой: {cer_err}"
        return result

    if cerberus_data:
        # Запись найдена – заполняем поля
        result["destination"] = cerberus_data.get("destination", "-")
        result["enterprise_status"] = cerberus_data.get("status", "-")
        # Оформляем `info`
        result["info"] = _compose_info_block(
            inn=inn,
            destination=result["destination"],
            status=result["enterprise_status"],
            found_keywords=found_keywords,
            validity=result["validity"]
        )
        return result

    # -------------------------------------------------------------
    # 5. Поиск по названию (если по ИНН ничего не нашли)
    # -------------------------------------------------------------
    # Пытаемся извлечь название предприятия из PDF‑текста.
    # На некоторых сертификатах название указано в строке "Организм: <название>"
    # или в начале документа. Попробуем взять первые 200 символов и
    # отфильтровать «лишний» мусор.
    name_candidate = _extract_company_name(pdf_text)

    if name_candidate:
        cerberus_data2, cer_err2 = search_cerberus_by_name(name_candidate)
        if cer_err2:
            result["info"] = (
                f"ИНН ({inn}) не найден в Cerberus, а поиск по названию "
                f"`{name_candidate}` завершился ошибкой: {cer_err2}"
            )
        elif cerberus_data2:
            # Нашли запись по названию!
            result["destination"] = cerberus_data2.get("destination", "-")
            result["enterprise_status"] = cerberus_data2.get("status", "-")
            result["info"] = _compose_info_block(
                inn=inn,
                destination=result["destination"],
                status=result["enterprise_status"],
                found_keywords=found_keywords,
                validity=result["validity"],
                note="Найдено по названию компании"
            )
        else:
            # Ни ИНН, ни название не привели к записи
            result["info"] = (
                f"ИНН ({inn}) не найден в Cerberus, "
                f"по названию `{name_candidate}` запись также отсутствует.\n"
                f"{'Ключевые слова: ' + ', '.join(found_keywords) if found_keywords else 'Ключевые слова не найдены'}\n"
                f"{result['validity']}"
            )
    else:
        # Не удалось извлечь название – просто сообщаем о пустом результате
        result["info"] = (
            f"ИНН ({inn}) не найден в Cerberus, а название компании из PDF не удалось извлечь.\n"
            f"{'Ключевые слова: ' + ', '.join(found_keywords) if found_keywords else 'Ключевые слова не найдены'}\n"
            f"{result['validity']}"
        )

    return result


def _compose_info_block(
    inn: str,
    destination: str,
    status: str,
    found_keywords: list[str],
    validity: str,
    note: str | None = None,
) -> str:
    """Универсальный формирователь текста в поле `info`."""
    parts = [
        f"ИНН: {inn}",
        f"Страна назначения: {destination}",
        f"Статус предприятия: {status}",
    ]
    if note:
        parts.append(f"Примечание: {note}")
    if found_keywords:
        parts.append(f"Ключевые слова: {', '.join(found_keywords)}")
    else:
        parts.append("Ключевые слова не найдены")
    parts.append(validity)
    return "\n".join(parts)


def _extract_company_name(text: str) -> str | None:
    """
    Очень простенький «эвристический» извлекатель названия.
    Ищем фрагменты в кавычках, строки вида «Наименование: …», «Название: …».
    Если ничего не найдено – возвращаем None.
    """
    # 1) Попытка найти название в кавычках («…» или "…")
    m = re.search(r"[«\"']([^«\"']{3,60})[»\"']", text)
    if m:
        return m.group(1).strip()

    # 2) «Наименование: ...» (часто в нижних колонтитулах)
    m = re.search(r"(?:Наименование|Название|Организм)\s*[:‑]\s*([^\n]{3,70})", text, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()

    # 3) Если в документе в начале встречается строка, где есть слово “LTD” / “LLC”:
    m = re.search(r"([A-ZА-ЯЁ0-9\s\.\-]{5,70})(?:\s+L\.?T\.?D\.?|LLC|Ltd\.?)", text, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()

    return None


# ----------------------------------------------------------------------
# Flask‑эндпоинты
# ----------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/check", methods=["POST"])
def check():
    data = request.get_json(silent=True) or {}
    cert_numbers = data.get("numbers", [])
    keywords = data.get("keywords", KEYWORDS_DEFAULT)

    results = [analyze_certificate(num, keywords) for num in cert_numbers]
    return jsonify({"results": results})


if __name__ == "__main__":
    # В продакшене отключайте debug!
    app.run(host="0.0.0.0", port=10000, debug=True)