
from flask import Flask, render_template, request, jsonify
import os
import re
import fitz  # from PyMuPDF
import requests
from datetime import datetime, timedelta
from flask_cors import CORS
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC

app = Flask(__name__, template_folder="templates")
CORS(app)

PDF_IFRAME_URL = "https://new.fitorf.ru/validate/ks/{}/print"
CERBERUS_URL = "https://cerberus.vetrf.ru/cerberus/certified/exporter/pub"
KEYWORDS_DEFAULT = ["Эгилопс"]
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def extract_issue_date(text):
    match = re.search(r"(?:выдан[а-я]*|дата выдачи|оформлен[а-я]*)[^0-9]{0,20}(\d{2}\.\d{2}\.\d{4})", text, re.IGNORECASE)
    if match:
        return match.group(1)
    dates = re.findall(r"\d{2}\.\d{2}\.\d{4}", text)
    return dates[-1] if dates else None


def extract_inn(text):
    match = re.search(r"\b\d{10}\b", text)
    return match.group(0) if match else None


def check_cerberus(inn):
    try:
        options = webdriver.ChromeOptions()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.binary_location = os.getenv("GOOGLE_CHROME_BIN", "/usr/bin/google-chrome")

        driver = webdriver.Chrome(options=options)
        driver.get(CERBERUS_URL)

        wait = WebDriverWait(driver, 10)
        inn_field = wait.until(EC.presence_of_element_located((By.ID, "businessEntityInnOgrn")))
        inn_field.clear()
        inn_field.send_keys(inn)

        Select(driver.find_element(By.ID, "exporterCountries")).select_by_visible_text("Китай")
        driver.find_element(By.ID, "searchBtn").click()

        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr")))
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        if not rows:
            return "Не найден в Cerberus"

        for row in rows:
            cells = row.find_elements(By.TAG_NAME, "td")
            if len(cells) >= 5 and "Китай" in cells[3].text:
                return cells[-1].text.strip()

        return "Нет аккредитации на Китай"
    except Exception as e:
        return f"Ошибка Cerberus: {e}"
    finally:
        try:
            driver.quit()
        except:
            pass


def fetch_pdf_text(cert_number):
    try:
        url = PDF_IFRAME_URL.format(cert_number)
        response = requests.get(url, headers=HEADERS)
        if response.status_code != 200:
            return None, "PDF не найден в iframe"

        path = f"temp_{cert_number}.pdf"
        with open(path, "wb") as f:
            f.write(response.content)

        with fitz.open(path) as doc:
            text = "\n".join(page.get_text() for page in doc)

        os.remove(path)
        return text, None
    except Exception as e:
        return None, f"Ошибка загрузки PDF: {str(e)}"


def analyze_certificate(cert_number, keywords):
    result = {
        "number": cert_number,
        "found": False,
        "info": "PDF не найден",
        "destination": "-",
        "enterprise_status": "-",
        "valid": None,
    }

    text, error = fetch_pdf_text(cert_number)
    if error:
        result["info"] = error
        return result

    found_keywords = [kw for kw in keywords if re.search(re.escape(kw), text, re.IGNORECASE)]
    result["found"] = bool(found_keywords)

    issue_date_str = extract_issue_date(text)
    if issue_date_str:
        try:
            issue_date = datetime.strptime(issue_date_str, "%d.%m.%Y")
            now = datetime.now()
            if issue_date > now:
                result["valid"] = True
                validity_text = f"Сертификат действующий (выдан {issue_date_str})"
            elif now - issue_date <= timedelta(days=365):
                result["valid"] = True
                validity_text = f"Сертификат действующий (выдан {issue_date_str})"
            else:
                result["valid"] = False
                validity_text = f"Сертификат просрочен (выдан {issue_date_str})"
        except Exception:
            validity_text = f"Не удалось разобрать дату: {issue_date_str}"
    else:
        validity_text = "Дата выдачи не найдена"

    inn = extract_inn(text)
    if inn:
        status = check_cerberus(inn)
        result["enterprise_status"] = status
    else:
        status = "ИНН не найден на Cerberus"

    result["destination"] = "Китай"
    detail = f"Найдено: {', '.join(found_keywords)}" if found_keywords else "Ключевые слова не найдены"
    result["info"] = f"{detail}\n{validity_text}\n{status}"
    return result


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/check", methods=["POST"])
def check():
    try:
        data = request.get_json()
        cert_numbers = data.get("numbers", [])
        keywords = data.get("keywords", KEYWORDS_DEFAULT)
        results = [analyze_certificate(num, keywords) for num in cert_numbers]
        return jsonify({"results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
