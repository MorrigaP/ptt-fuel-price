import requests
from bs4 import BeautifulSoup
import csv
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

URL = "https://oilprice.ryangl.com/en/"
NGV_URL = "https://www.pttplc.com/th"

# ===== PTT OR SOAP web service (primary source) =====
PTTOR_API_URL = "https://orapiweb.pttor.com/oilservice/OilPrice.asmx"
PTTOR_SOAP_ACTION = "https://orapiweb.pttor.com/CurrentOilPrice"
PTTOR_NAMESPACE = "http://www.pttor.com"
PTTOR_LANGUAGES = ["EN", "TH"]      # try EN first, then TH if product names don't map
MIN_FUEL_ROWS = 5                   # same threshold the scraper already used

NAME_TO_FUEL_TYPE = {
    "Diesel B20": "diesel_b20",
    "Diesel": "diesel",
    "Gasohol E20": "gasohol_e20",
    "Gasohol 91": "gasohol_91",
    "Gasohol 95": "gasohol_95",
    "Gasoline 95": "gasoline_95",
    "Premium Diesel": "premium_diesel",
    "Super Power GSH95": "superpower_gasohol_95",
    "ngv":"ngv"
}

# ===== Map PTT OR product names -> fuel_type =====
# Matching is done on the normalized name (lowercase, punctuation/space stripped).
# Each entry: (groups, fuel_type, default_thai_name)
#   - every group must match; within a group any one alternative is enough
# Order matters: most specific first, because the first match wins.
PTTOR_PATTERNS = [
    ((("superpower", "ซูเปอร์พาวเวอร์"), ("gasohol95", "gsh95", "แก๊สโซฮอล์95")),
     "superpower_gasohol_95", "ซูเปอร์ พาวเวอร์ แก๊สโซฮอล์ 95"),
    ((("superpower", "premium", "hyforce", "ซูเปอร์พาวเวอร์", "พรีเมียม"), ("diesel", "ดีเซล")),
     "premium_diesel", "พรีเมียม ดีเซล"),
    ((("diesel", "ดีเซล"), ("b20", "บี20")),
     "diesel_b20", "ดีเซล B20"),
    ((("gasohol", "แก๊สโซฮอล์"), ("e20", "อี20")),
     "gasohol_e20", "แก๊สโซฮอล์ E20"),
    ((("gasohol", "แก๊สโซฮอล์"), ("91",)),
     "gasohol_91", "แก๊สโซฮอล์ 91"),
    ((("gasohol", "แก๊สโซฮอล์"), ("95",)),
     "gasohol_95", "แก๊สโซฮอล์ 95"),
    ((("gasoline", "benzine", "ulg", "เบนซิน"), ("95",)),
     "gasoline_95", "เบนซิน 95"),
    ((("diesel", "ดีเซล"),),
     "diesel", "ดีเซล"),
    ((("ngv",),),
     "ngv", "แก๊ส NGV"),
]

# ===== แปลงวันที่เป็นภาษาไทย พ.ศ. (ให้ตรงกับข้อมูลเก่า) =====
THAI_MONTHS = {
    1: "มกราคม", 2: "กุมภาพันธ์", 3: "มีนาคม", 4: "เมษายน",
    5: "พฤษภาคม", 6: "มิถุนายน", 7: "กรกฎาคม", 8: "สิงหาคม",
    9: "กันยายน", 10: "ตุลาคม", 11: "พฤศจิกายน", 12: "ธันวาคม",
}

def to_thai_date(dt):
    """แปลง datetime -> '7 กรกฎาคม 2569' (พ.ศ.)"""
    day = dt.day
    month_th = THAI_MONTHS[dt.month]
    year_be = dt.year + 543  # ค.ศ. -> พ.ศ.
    return f"{day} {month_th} {year_be}"


def read_last_prices(path):
    """
    คืน (last_prices, last_names) จากแถวล่าสุดของแต่ละชนิดใน CSV เดิม
      last_prices = {fuel_type: price}
      last_names  = {fuel_type: fuel_name_th}   <- ใช้ให้ชื่อไทยจาก API ตรงกับข้อมูลเดิม
    """
    last = {}
    names = {}
    if not os.path.exists(path):
        return last, names
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                ftype = r.get("fuel_type")
                if not ftype:
                    continue
                name_th = (r.get("fuel_name_th") or "").strip()
                if name_th:
                    names[ftype] = name_th
                try:
                    last[ftype] = float(r["price"])
                except (ValueError, TypeError, KeyError):
                    continue
    except Exception as e:
        print(f"⚠️ อ่าน CSV เดิมไม่ได้: {type(e).__name__}: {e}")
    return last, names


# ===== ดึงราคาน้ำมันจาก API ของ PTT OR (แหล่งหลัก) =====
def _normalize_product(name):
    """lowercase + ตัดทุกอย่างที่ไม่ใช่ a-z, 0-9 หรืออักษรไทย เพื่อให้ match ได้ทนทาน"""
    return re.sub(r"[^a-z0-9\u0e00-\u0e7f]", "", (name or "").lower())


def _match_fuel_type(product_name):
    """คืน (fuel_type, default_thai_name) หรือ (None, None) ถ้าไม่รู้จัก"""
    norm = _normalize_product(product_name)
    if not norm:
        return None, None
    for groups, ftype, th_name in PTTOR_PATTERNS:
        if all(any(alt in norm for alt in group) for group in groups):
            return ftype, th_name
    return None, None


def _call_pttor(language):
    """ยิง SOAP 1.1 ไปที่ CurrentOilPrice แล้วคืน list ของ dict ที่ parse แล้ว"""
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
        ' xmlns:xsd="http://www.w3.org/2001/XMLSchema"'
        ' xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soap:Body>"
        f'<CurrentOilPrice xmlns="{PTTOR_NAMESPACE}">'
        f"<Language>{language}</Language>"
        "</CurrentOilPrice>"
        "</soap:Body>"
        "</soap:Envelope>"
    )
    headers = {
        "Content-Type": "text/xml; charset=utf-8",
        "SOAPAction": f'"{PTTOR_SOAP_ACTION}"',
        "User-Agent": "Mozilla/5.0",
    }

    last_err = None
    for attempt in range(2):   # retry once on transient network errors
        try:
            resp = requests.post(PTTOR_API_URL, data=envelope.encode("utf-8"),
                                 headers=headers, timeout=30)
            resp.raise_for_status()
            break
        except Exception as e:
            last_err = e
            if attempt == 0:
                time.sleep(3)
    else:
        raise last_err

    # ชั้นที่ 1: SOAP envelope -> เอาข้อความใน <CurrentOilPriceResult>
    root = ET.fromstring(resp.content)
    result_text = None
    for el in root.iter():
        if el.tag.split("}")[-1] == "CurrentOilPriceResult":
            result_text = (el.text or "").strip()
            break
    if not result_text:
        raise ValueError("PTT OR API: no CurrentOilPriceResult in SOAP response")

    # ชั้นที่ 2: ข้างในเป็น XML dataset (<PTT_DS><DataAccess>...)
    inner = ET.fromstring(result_text)
    items = []
    for node in inner.iter():
        if node.tag.split("}")[-1] != "DataAccess":
            continue
        rec = {}
        for child in node:
            rec[child.tag.split("}")[-1].upper()] = (child.text or "").strip()
        items.append(rec)

    if not items:
        raise ValueError("PTT OR API: no DataAccess rows in result")
    return items


def fetch_from_pttor_api(capture_date, price_date_th, last_names):
    """
    แหล่งหลัก: SOAP API ของ PTT OR
    คืน (rows, ngv_price, warnings)  — raise ถ้าเรียก API ไม่สำเร็จเลย (ให้ caller ไป fallback)
    """
    warnings = []
    best_rows, best_ngv, best_unknown, best_lang = [], None, [], None

    last_err = None
    for language in PTTOR_LANGUAGES:
        try:
            items = _call_pttor(language)
        except Exception as e:
            last_err = e
            warnings.append(f"⚠️ PTT OR API ({language}): {type(e).__name__}: {e}")
            continue

        rows, ngv_price, unknown, seen = [], None, [], set()
        for rec in items:
            product = rec.get("PRODUCT", "")
            price_raw = rec.get("PRICE", "")
            if not product:
                continue
            if not price_raw:
                continue   # สินค้าที่เลิกขายแล้วจะไม่มี PRICE

            try:
                price_val = float(price_raw.replace(",", ""))
            except ValueError:
                warnings.append(f"⚠️ PTT OR API invalid price for '{product}': '{price_raw}'")
                continue
            if not (1.0 <= price_val <= 200.0):
                warnings.append(f"⚠️ PTT OR API price out of range for '{product}': {price_val}")
                continue

            ftype, default_th = _match_fuel_type(product)
            if ftype is None:
                unknown.append(product)
                continue
            if ftype in seen:
                continue   # กันซ้ำ เช่น API ส่ง Diesel B7 และ Diesel มาพร้อมกัน
            seen.add(ftype)

            # ราคาค้างเก่า -> เตือนไว้ แต่ยังใช้ (ราคาบางชนิดนิ่งได้เป็นเดือน)
            price_date_raw = rec.get("PRICE_DATE", "")
            if price_date_raw:
                try:
                    pd_dt = datetime.fromisoformat(price_date_raw)
                    age_days = (datetime.now(timezone(timedelta(hours=7))) - pd_dt).days
                    if age_days > 90:
                        warnings.append(f"⚠️ PTT OR API stale price for '{product}': {price_date_raw} ({age_days} days old)")
                except ValueError:
                    pass

            if ftype == "ngv":
                ngv_price = f"{price_val:.2f}"
                continue   # NGV เก็บแยก ใช้เป็นตัวสำรองของ Playwright เท่านั้น

            rows.append({
                "capture_date": capture_date,
                "price_date_th": price_date_th,
                "company": "PTT",
                "fuel_type": ftype,
                # ใช้ชื่อไทยจาก CSV เดิมก่อน เพื่อให้ข้อมูลต่อเนื่องกับของเก่า
                "fuel_name_th": last_names.get(ftype) or default_th,
                "price": f"{price_val:.2f}",
            })

        if len(rows) > len(best_rows):
            best_rows, best_ngv, best_unknown, best_lang = rows, ngv_price, unknown, language
        if len(best_rows) >= MIN_FUEL_ROWS:
            break

    if not best_rows and last_err is not None:
        raise last_err

    if best_unknown:
        warnings.append(f"⚠️ PTT OR API unmapped products: {best_unknown}")
    if best_lang:
        warnings.append(f"ℹ️ PTT OR API language used: {best_lang}")

    return best_rows, best_ngv, warnings


# ===== ดึงราคาน้ำมันจาก oilprice.ryangl.com (ตัวสำรอง - ของเดิม) =====
def fetch_from_ryangl(capture_date, price_date_th):
    """แหล่งสำรอง: scrape เว็บเดิม — ตรรกะเดิมทั้งหมด ไม่แก้"""
    warnings = []
    rows = []

    resp = requests.get(URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    table = soup.find("table")
    if table is None:
        raise ValueError("ERROR: ไม่พบ table ในหน้าเว็บ - โครงสร้างอาจเปลี่ยน")

    for tr in table.find_all("tr")[1:]:
        cells = tr.find_all("td")
        if len(cells) < 2:
            continue

        fuel_cell = cells[0].get_text(strip=True)
        price_today = cells[1].get_text(strip=True)

        matched_type = None
        matched_en = None
        for en_name, ftype in NAME_TO_FUEL_TYPE.items():
            if fuel_cell.startswith(en_name):
                matched_type = ftype
                matched_en = en_name
                break

        if matched_type is None:
            warnings.append(f"⚠️ UNKNOWN fuel: '{fuel_cell}'")
            matched_type = "unknown"
            matched_en = fuel_cell

        name_th = fuel_cell.replace(matched_en, "").strip() if matched_en else fuel_cell

        try:
            price_val = float(price_today)
        except ValueError:
            warnings.append(f"⚠️ INVALID price for '{fuel_cell}': '{price_today}' - skipped")
            continue

        rows.append({
            "capture_date": capture_date,
            "price_date_th": price_date_th,   # ← ใช้ format ไทยแล้ว
            "company": "PTT",
            "fuel_type": matched_type,
            "fuel_name_th": name_th,
            "price": price_today,
        })

    if len(rows) < MIN_FUEL_ROWS:
        raise ValueError(f"ERROR: ได้แค่ {len(rows)} แถว - หยุดไม่เขียน CSV")

    return rows, warnings


# ===== ดึงราคา NGV จากหน้า ปตท. ด้วย headless browser =====
def fetch_ngv():
    """
    คืนราคา NGV เป็น string เช่น '20.00' หรือ None ถ้าดึงไม่ได้

    ต้องใช้ browser จริงเพราะ:
      1) หน้า pttplc.com เป็น Next.js — div.popupStockPrice ถูก JS สร้างทีหลัง
      2) เว็บมี bot protection ที่ต้องรัน JS ผ่านก่อนถึงจะเห็นเนื้อหาจริง
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        try:
            context = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
                viewport={"width": 1440, "height": 900},
                locale="th-TH",
                timezone_id="Asia/Bangkok",
            )
            page = context.new_page()
            page.goto(NGV_URL, wait_until="domcontentloaded", timeout=60000)

            # รอให้ราคาโผล่ (เผื่อต้องผ่านหน้า challenge ก่อน จึงให้เวลา 45 วิ)
            page.wait_for_selector(".popupStockPrice", timeout=45000)

            # จับคู่ด้วย symbol เท่านั้น — กล่องแรกคือราคาหุ้น PTT (38.50) ไม่ใช่ NGV
            for box in page.query_selector_all(".popupStockBody, .popupStockContainer"):
                sym = box.query_selector(".popupStockSymbol")
                price = box.query_selector(".popupStockPrice")
                if sym and price and sym.inner_text().strip().upper() == "NGV":
                    value = price.inner_text().strip().replace(",", "")
                    if 5.0 <= float(value) <= 100.0:   # กันค่าเพี้ยน
                        return f"{float(value):.2f}"

            # หาไม่เจอ — พิมพ์ข้อมูลช่วย debug ลง log
            print(f"[NGV DEBUG] title={page.title()!r}")
            print(f"[NGV DEBUG] symbols={[e.inner_text().strip() for e in page.query_selector_all('.popupStockSymbol')]}")
            print(f"[NGV DEBUG] prices={[e.inner_text().strip() for e in page.query_selector_all('.popupStockPrice')]}")
            return None
        finally:
            browser.close()


# ===== MAIN =====
# วันที่ปัจจุบัน (เวลาไทย)
now_th = datetime.now(timezone(timedelta(hours=7)))
capture_date = now_th.strftime("%Y-%m-%d")
price_date_th = to_thai_date(now_th)   # ← format ไทย พ.ศ. เหมือนข้อมูลเก่า

csv_path = "fuel_prices.csv"
last_prices, last_names = read_last_prices(csv_path)   # ← ต้องอ่านก่อน append

rows = []
warnings = []
api_ngv_price = None
source = None

# 1) ลอง API ของ PTT OR ก่อน
if os.environ.get("SIMULATE_API_FAIL") == "1":
    print("⚠️ SIMULATED: PTT OR API failure")
    warnings.append("⚠️ SIMULATED: PTT OR API failure")
else:
    try:
        api_rows, api_ngv_price, api_warnings = fetch_from_pttor_api(
            capture_date, price_date_th, last_names
        )
        warnings += api_warnings
        if len(api_rows) >= MIN_FUEL_ROWS:
            rows = api_rows
            source = "PTT OR API (orapiweb.pttor.com)"
            print(f"✅ PTT OR API: ได้ {len(api_rows)} แถว")
        else:
            msg = f"⚠️ PTT OR API ใช้ได้แค่ {len(api_rows)} แถว (ต้องการ {MIN_FUEL_ROWS}) - ไปใช้ตัวสำรอง"
            print(msg)
            warnings.append(msg)
    except Exception as e:
        msg = f"⚠️ PTT OR API error: {type(e).__name__}: {e}"
        print(msg)
        warnings.append(msg)

# 2) ถ้า API ไม่ได้ผล ค่อยไป scrape เว็บเดิม
if not rows:
    try:
        rows, scrape_warnings = fetch_from_ryangl(capture_date, price_date_th)
        warnings += scrape_warnings
        source = "oilprice.ryangl.com (fallback)"
        print(f"✅ oilprice.ryangl.com (fallback): ได้ {len(rows)} แถว")
    except Exception as e:
        msg = f"⚠️ oilprice.ryangl.com (fallback) error: {type(e).__name__}: {e}"
        print(msg)
        warnings.append(msg)

# ทั้งสองแหล่งล้มเหลว - หยุดก่อนเขียน CSV แต่เขียน commit_msg.txt ให้มีรายละเอียด
# ครบ เพื่อให้อีเมลแจ้งเตือนบอกได้ว่าล้มเพราะอะไร แทนที่จะเป็นข้อความ generic
if not rows:
    print("=" * 50)
    print("ERROR: ทั้ง PTT OR API และตัวสำรองล้มเหลว - ไม่มีข้อมูลให้เขียน")
    print("=" * 50)
    error_msg = [
        f"ดึงราคาน้ำมันไม่สำเร็จ ({capture_date})",
        "",
        "ทั้งแหล่งหลัก (PTT OR API) และตัวสำรอง (oilprice.ryangl.com) ล้มเหลว:",
        "",
    ] + warnings
    with open("commit_msg.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(error_msg) + "\n")
    sys.exit(1)

print(f"SOURCE: {source}")

# ===== เพิ่มแถว NGV (หลังเช็คจำนวนแถวน้ำมันแล้ว) =====
# ครอบ try ไว้ เพราะ NGV มาคนละเว็บ ถ้ามันล่มไม่ควรทำให้ราคาน้ำมันหายไปทั้งวัน
try:
    ngv_price = fetch_ngv()
except Exception as e:
    ngv_price = None
    warnings.append(f"⚠️ NGV fetch error: {type(e).__name__}: {e}")

# ถ้า Playwright ล้มเหลว แต่ API ส่ง NGV มาด้วย ก็ใช้ของ API แทน
if not ngv_price and api_ngv_price:
    ngv_price = api_ngv_price
    warnings.append("ℹ️ NGV มาจาก PTT OR API (scrape pttplc.com ไม่สำเร็จ)")

if ngv_price:
    rows.append({
        "capture_date": capture_date,
        "price_date_th": price_date_th,
        "company": "PTT",
        "fuel_type": NAME_TO_FUEL_TYPE["ngv"],
        "fuel_name_th": "แก๊ส NGV",
        "price": ngv_price,   # หมายเหตุ: NGV เป็นบาท/กก. ส่วนที่เหลือเป็นบาท/ลิตร
    })
else:
    warnings.append("⚠️ NGV not available")

if warnings:
    print("=" * 50)
    print("DATA QUALITY WARNINGS:")
    for w in warnings:
        print(w)
    print("=" * 50)

file_exists = os.path.exists(csv_path)

fieldnames = ["capture_date", "price_date_th", "company", "fuel_type", "fuel_name_th", "price"]

with open(csv_path, "a", newline="", encoding="utf-8-sig") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    if not file_exists:
        writer.writeheader()
    writer.writerows(rows)

print(f"Wrote {len(rows)} rows for {capture_date} ({price_date_th})")
for r in rows:
    print(f"  {r['fuel_type']}: {r['fuel_name_th']} = {r['price']}")

# ===== สร้าง commit message พร้อมส่วนต่างราคา =====
msg = [f"Update fuel prices {price_date_th}", "", f"source: {source}", ""]
for r in rows:
    cur = float(r["price"])
    prev = last_prices.get(r["fuel_type"])
    if prev is None:
        delta = "(ใหม่)"
    elif abs(cur - prev) < 0.005:
        delta = "(เท่าเดิม)"
    else:
        delta = f"({cur - prev:+.2f})"
    msg.append(f"{r['fuel_name_th'] or r['fuel_type']}: {cur:.2f} {delta}")

if warnings:
    msg += [""] + warnings

with open("commit_msg.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(msg) + "\n")
