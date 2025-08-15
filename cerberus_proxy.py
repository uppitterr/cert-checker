from flask import Flask, request, jsonify
from playwright.sync_api import sync_playwright

app = Flask(__name__)

def get_cerberus_data(inn: str):
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            page = browser.new_page()
            url = f"https://cerberus.vetrf.ru/cerberus/certified/exporter/pub?businessEntityInnOgrn={inn}"
            page.goto(url, timeout=60000)
            page.wait_for_load_state("networkidle")
            page.wait_for_selector("table.table", timeout=15000)

            rows = page.query_selector_all("table.table tbody tr")
            if not rows:
                browser.close()
                return {"error": "Предприятие не найдено"}

            cols = [cell.inner_text().strip() for cell in rows[0].query_selector_all("td")]
            browser.close()

            if len(cols) < 6:
                return {"error": "Не удалось распарсить таблицу"}

            destination = cols[4]
            status = cols[5]
            china_accredited = "китай" in destination.lower()

            return {
                "inn": inn,
                "destination": destination,
                "status": status,
                "china_accredited": china_accredited,
                "china_status": "Аккредитован на Китай" if china_accredited else "Не аккредитован на Китай"
            }
    except Exception as e:
        return {"error": str(e)}

@app.route("/cerberus", methods=["GET"])
def cerberus():
    inn = request.args.get("inn")
    if not inn:
        return jsonify({"error": "ИНН не указан"}), 400
    return jsonify(get_cerberus_data(inn))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)