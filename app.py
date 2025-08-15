# -*- coding: utf-8 -*-
import os
import re
import time
import fitz  # PyMuPDF
import requests
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

PDF_IFRAME_URL = "https://new.fitorf.ru/validate/ks/{}/print"
KEYWORDS_DEFAULT = ["Эгилопс"]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9"
}
TIMEOUT = 30
MAX_RETRIES = 3

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
adapter = requests.adapters.HTTPAdapter(max_retries=MAX_RETRIES)
SESSION.mount("http://", adapter)
SESSION.mount("https://", adapter)

# ---------------------- ВСПОМОГАТЕЛЬНЫЕ ----------------------

def _normalize_lines(text: str):
    return [ln.strip() for ln in text.splitlines() if ln.strip()]

def extract_issue_date(text: str):
    patterns = [
        r"(?:выдан[а-я]*|дата выдачи|оформлен[а-я]*)[^0-9]{0,20}(\d{2}\.\d{2}\.\d{4})",
        r"дата:\s*(\d{2}\.\d{2}\.\d{4})",
        r"от\s*(\d{2}\.\д{2}\.\д{4})"
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            return m.group(1)
    dates = re.findall(r"\d{2}\.\d{2}\.\d{4}", text)
    return dates[-1] if dates else None

def fetch_pdf_text(cert_number: str):
    for attempt in range(MAX_RETRIES):
        try:
            url = PDF_IFRAME_URL.format(cert_number)
            resp = SESSION.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            with fitz.open(stream=resp.content, filetype="pdf") as doc:
                text = "\n".join(page.get_text() for page in doc)
            return text, None
        except requests.exceptions.RequestException as e:
            if attempt == MAX_RETRIES - 1:
                return None, f"Ошибка сети: {str(e)}"
            time.sleep(1)
        except Exception as e:
            return None, f"Ошибка обработки PDF: {str(e)}"
    return None, "Не удалось загрузить PDF"

def search_keywords(text: str, keywords: list[str]):
    found = []
    for kw in keywords:
        if kw and re.search(re.escape(kw), text, re.IGNORECASE):
            found.append(kw)
    return bool(found), found

# ---------------------- ИЗВЛЕЧЕНИЕ ОТПРАВИТЕЛЯ ----------------------

_ADDRESS_TOKENS = (
    r"россия\b|рф\b|респ\.?|край\b|обл\.?|район\b|г\.\s|пос[\. ]|д\.\s|ул\.\s|просп\.\s|пер\.\s|дом\b|кв\.\b|стр\.\b|индекс\b"
)
_STOP_HEADERS = (
    r"получатель|recipient|receiver|грузополучатель|наименование|вид упаковки|страна|рег(\.|истрационный)?\s*номер|сертификат"
)

def _cleanup_name(s: str):
    # убрать содержимое скобок и ИНН/ОГРН из хвоста
    s = re.sub(r"\(.*?\)", "", s)
    s = re.sub(r"ИНН\s*:?\s*\d{10,12}", "", s, flags=re.IGNORECASE)
    s = re.sub(r"ОГРН[ИП]?\s*:?\s*\d{10,15}", "", s, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", s).strip(" ,;—-")

def _looks_like_address(s: str):
    if re.search(_ADDRESS_TOKENS, s, flags=re.IGNORECASE):
        return True
    if re.search(r"\b\d{6}\b", s):  # почтовый индекс
        return True
    return False

def _is_good_person_or_org(s: str):
    if not s or len(s) < 4:
        return False
    bad = ("сертификат", "certificate")
    if any(w in s.lower() for w in bad):
        return False
    # Должны быть буквы (не только цифры/знаки)
    return bool(re.search(r"[A-Za-zА-Яа-яЁё]", s))

def _extract_sender_name(text: str):
    """
    Ищет блок вида:
    'Отправитель подкарантинной продукции (груза, материала) и его адрес: ИП ...'
    или на следующей строке после заголовка.
    """
    lines = _normalize_lines(text)

    # 1) Попытка выцепить прямо из строки после двоеточия
    for i, line in enumerate(lines):
        if re.search(r"отправитель", line, flags=re.IGNORECASE):
            # максимально терпимый заголовок
            if not re.search(r":", line):
                # нет двоеточия — берём одну из следующих строк
                for j in range(1, 4):
                    if i + j >= len(lines):
                        break
                    cand = _cleanup_name(lines[i + j])
                    if not cand or _looks_like_address(cand):
                        continue
                    if _is_good_person_or_org(cand):
                        # если в следующей строке идёт адрес — не берём
                        # (или берём только левую часть до запятой/слова "Россия")
                        left = re.split(r",|\bРоссия\b|\bРФ\b", cand, maxsplit=1, flags=re.IGNORECASE)[0].strip()
                        return left if _is_good_person_or_org(left) else cand
            else:
                after = line.split(":", 1)[1].strip()
                after = _cleanup_name(after)
                if after:
                    # В той же строке часто после имени сразу адрес — отрежем по признакам адреса
                    left = re.split(r",|\bРоссия\b|\bРФ\b", after, maxsplit=1, flags=re.IGNORECASE)[0].strip()
                    if _is_good_person_or_org(left):
                        return left
                # Если после двоеточия пусто/служебка — смотрим вниз
                for j in range(1, 4):
                    if i + j >= len(lines):
                        break
                    cand = _cleanup_name(lines[i + j])
                    if not cand or _looks_like_address(cand):
                        continue
                    if re.search(_STOP_HEADERS, cand, flags=re.IGNORECASE):
                        break
                    if _is_good_person_or_org(cand):
                        left = re.split(r",|\bРоссия\b|\bРФ\b", cand, maxsplit=1, flags=re.IGNORECASE)[0].strip()
                        return left if _is_good_person_or_org(left) else cand
    return None

# ---------------------- ИЗВЛЕЧЕНИЕ НАЗВАНИЯ КУЛЬТУРЫ ----------------------

def extract_product_name(text: str):
    """
    Извлекает наименование подкарантинной продукции.
    Учитывает случаи, когда рядом заголовок 'Количество (объем)' и переносы строк.
    """
    lines = _normalize_lines(text)
    service_words = {"количество", "количество (объем)", "объем", "вес", "масса"}

    # Ключевые заголовки, около которых встречается нужное поле
    header_regex = re.compile(
        r"наименование\s+подкарантинной\s+продукции|"
        r"наименование\s+груза|"
        r"наименование\s+материала|"
        r"product\s+name|"
        r"наименование(?!.*организации)",  # простое "Наименование", но не "организации"
        flags=re.IGNORECASE
    )

    for i, line in enumerate(lines):
        if header_regex.search(line):
            # 1) Текст в той же строке после двоеточия
            after = re.sub(r".*?:", "", line, count=1).strip() if ":" in line else ""
            after = re.sub(r"\(.*?\)", "", after).strip()
            if after and after.lower() not in service_words and len(after) > 3:
                return after[:200]

            # 2) Берём следующую содержательную строку, игнорируя служебные слова/столбцы
            for j in range(1, 5):
                if i + j >= len(lines):
                    break
                cand = re.sub(r"\(.*?\)", "", lines[i + j]).strip()
                low = cand.lower()
                if not cand or low in service_words:
                    continue
                # частый случай: справа количество — срезаем по двум+ пробелам или по "  " либо по числу+ед.
                cand = re.split(r"\s{2,}|\s\d+(\,\d+)?\s?(т|кг|тонн|шт)\b", cand, maxsplit=1)[0].strip()
                if len(cand) > 3:
                    return cand[:200]

    # fallback: иногда поле называется иначе — пробуем простую эвристику
    m = re.search(
        r"(семена|зерн(о|а)|боб[ыа]|пшениц[аы]|ячмен[ья]|рож[ьи]|овёс|овес|горох|чечевица|кукуруза|лен|льн[аы]|рапс)[^;\n]{0,80}",
        text, flags=re.IGNORECASE)
    if m:
        return _cleanup_name(m.group(0))[:200]
    return None

# ---------------------- СТАТУС СЕРТИФИКАТА ----------------------

def extract_certificate_status(text: str):
    """Определяет статус сертификата (погашен/действует)"""
    if re.search(r'погашен|аннулирован|отозван', text, re.IGNORECASE):
        return "Погашен"
    if re.search(r'действует|активен|действующий', text, re.IGNORECASE):
        return "Действует"
    return None

# ---------------------- НАЗВАНИЕ КОМПАНИИ ----------------------

def _extract_company_name(text: str):
    """
    Приоритет:
    1) 'Отправитель подкарантинной продукции …' (и его адрес)
    2) По явным полям: отправитель/организация/компания/производитель/exporter/sender
    3) Эвристика по кавычкам
    4) Длинная осмысленная строка без служебных слов
    """
    try:
        # 1) Приоритет — явное поле Отправитель…
        sender = _extract_sender_name(text)
        if sender:
            return sender[:100]

        # 2) Явные поля (если вдруг в сертификате иная структура)
        patterns = [
            r'отправитель[:\s]*([^\n]{5,100})',
            r'организация[:\s]*([^\n]{5,100})',
            r'компания[:\s]*([^\n]{5,100})',
            r'наименование\s+организации[:\s]*([^\n]{5,100})',
            r'производитель[:\s]*([^\n]{5,100})',
            r'exporter[:\s]*([^\n]{5,100})',
            r'sender[:\s]*([^\n]{5,100})',
        ]
        for pattern in patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                name = _cleanup_name(m.group(1))
                if _is_good_person_or_org(name):
                    return name[:100]

        # 3) По кавычкам (меньший приоритет, чтобы не перехватывать «АГРОМЕР» вместо ИП)
        for quote_char in ['«', '»', '"', "'"]:
            for match in re.findall(rf'{quote_char}([^{quote_char}]+){quote_char}', text):
                clean = _cleanup_name(match)
                if _is_good_person_or_org(clean):
                    return clean[:100]

        # 4) Последняя эвристика
        for line in text.split('\n'):
            line = _cleanup_name(line)
            if len(line) > 30 and not re.search(r'сертификат|certificate|рег\.\s*номер', line, flags=re.IGNORECASE):
                return line[:100]
        return None
    except Exception:
        return None

# ---------------------- АНАЛИЗ СЕРТИФИКАТА ----------------------

def analyze_certificate(cert_number: str, keywords: list[str]):
    result = {
        "number": cert_number,
        "found": False,
        "found_keywords": [],
        "date_raw": "-",
        "inn": None,
        "company_name": None,
        "product_name": None,
        "cert_status": None,
        "validity": "-"
    }

    try:
        pdf_text, pdf_err = fetch_pdf_text(cert_number)
        if pdf_err:
            result["error"] = pdf_err
            return result

        # Ключевые слова
        found, found_keywords = search_keywords(pdf_text, keywords)
        result["found"] = found
        result["found_keywords"] = found_keywords

        # Дата и "валидность" по дате (эвристика)
        raw_date = extract_issue_date(pdf_text)
        result["date_raw"] = raw_date or "-"
        if raw_date:
            try:
                dt = datetime.strptime(raw_date, "%d.%m.%Y")
                result["validity"] = "Действующий" if dt >= datetime.now() - timedelta(days=30) else "Просрочен"
            except ValueError:
                result["validity"] = "Неверный формат даты"

        # ИНН: 10 (юрлица) или 12 (физлица/ИП)
        inn_match = re.search(r"ИНН\s*:?\s*(\d{10}|\d{12})", pdf_text, re.IGNORECASE)
        result["inn"] = inn_match.group(1) if inn_match else None

        # Отправитель/компания
        result["company_name"] = _extract_company_name(pdf_text)

        # Наименование продукции
        result["product_name"] = extract_product_name(pdf_text)

        # Статус сертификата (погашен/действует)
        result["cert_status"] = extract_certificate_status(pdf_text)

    except Exception as e:
        result["error"] = f"Критическая ошибка: {str(e)}"

    return result

# ---------------------- FLASK ----------------------

@app.route("/")
def index():
    return render_template("index.html")  # фронт уже показывает колонку 'Культура' (product_name) 

@app.route("/check", methods=["POST"])
def check():
    data = request.get_json() or {}
    cert_numbers = data.get("numbers", [])
    keywords = data.get("keywords", KEYWORDS_DEFAULT)

    if not cert_numbers:
        return jsonify({"error": "Не указаны номера сертификатов"}), 400

    results = []
    for num in cert_numbers:
        num = (num or "").strip()
        if not num:
            continue
        results.append(analyze_certificate(num, keywords))

    return jsonify({"results": results})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)