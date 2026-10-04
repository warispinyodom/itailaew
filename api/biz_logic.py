from datetime import datetime, timezone, timedelta
import re
try:
    from .config import VAT_RATE, SERVICE_CHARGE_RATE, RESERVATION_SLOT_MINUTES
except ImportError:
    from config import VAT_RATE, SERVICE_CHARGE_RATE, RESERVATION_SLOT_MINUTES

def now_iso():
    return datetime.now(timezone.utc).isoformat()

NAME_RE = re.compile(r"^[A-Za-z\u0E00-\u0E7F]+(?: +[A-Za-z\u0E00-\u0E7F]+)*$")
MENU_CATEGORIES = {"อาหาร", "ของหวาน", "เครื่องดื่ม"}
def validate_person_name(value, field="ชื่อ"):
    text = str(value or "").strip()
    if not text or not NAME_RE.fullmatch(text):
        raise ValueError(f"{field} ใช้ได้เฉพาะตัวอักษรไทย/อังกฤษและเว้นวรรค โดยห้ามเป็นเว้นวรรคอย่างเดียว")
    return text

def clean_text(value, field, max_len=120):
    if not isinstance(value, str):
        raise ValueError(f"{field} ต้องเป็นข้อความ")
    value = value.strip()
    if not value:
        raise ValueError(f"{field} ห้ามว่าง")
    if len(value) > max_len:
        raise ValueError(f"{field} ยาวเกินกำหนด")
    return value

def to_positive_number(value, field, allow_zero=False):
    try:
        number = float(value)
        if number < 0 or (number == 0 and not allow_zero):
            raise ValueError
        return number
    except (TypeError, ValueError):
        raise ValueError(f"{field} ต้องเป็นตัวเลขที่ถูกต้องและไม่ติดลบ")

def to_positive_int(value, field, allow_zero=False):
    try:
        number = int(value)
        if number < 0 or (number == 0 and not allow_zero):
            raise ValueError
        return number
    except (TypeError, ValueError):
        raise ValueError(f"{field} ต้องเป็นจำนวนเต็มที่ถูกต้อง")

def calculate_bill(items, discount=0, service_rate=SERVICE_CHARGE_RATE):
    try:
        subtotal = 0.0
        for item in items:
            qty = to_positive_int(item.get("quantity", 1), "จำนวน")
            price = to_positive_number(item.get("unit_price", 0), "ราคา")
            subtotal += price * qty
        discount = to_positive_number(discount or 0, "ส่วนลด", allow_zero=True)
        if discount > subtotal:
            raise ValueError("ส่วนลดต้องไม่เกินยอดรวม")
        service_charge = max(0.0, (subtotal - discount) * float(service_rate))
        taxable = max(0.0, subtotal - discount + service_charge)
        tax = taxable * VAT_RATE
        total = taxable + tax
        return {
            "subtotal": round(subtotal, 2), "discount": round(discount, 2),
            "service_charge": round(service_charge, 2), "tax": round(tax, 2),
            "total": round(total, 2)
        }
    except Exception as exc:
        raise ValueError(f"คำนวณบิลไม่สำเร็จ: {exc}")

def paginate(items, page=1, page_size=10):
    try:
        page = max(1, int(page))
        page_size = max(1, min(100, int(page_size)))
        total = len(items)
        start = (page - 1) * page_size
        return {
            "items": items[start:start + page_size],
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": max(1, (total + page_size - 1) // page_size)
        }
    except Exception as exc:
        raise ValueError(f"แบ่งหน้าไม่สำเร็จ: {exc}")

def search_filter_sort(items, query="", category="", sort="name"):
    try:
        query = (query or "").strip().lower()
        category = (category or "").strip().lower()
        result = []
        for item in items:
            name = str(item.get("name", "")).lower()
            cat = str(item.get("category", "")).lower()
            if query and query not in name:
                continue
            if category and cat != category:
                continue
            result.append(item)
        if sort == "price_asc":
            result.sort(key=lambda x: float(x.get("price", 0)))
        elif sort == "price_desc":
            result.sort(key=lambda x: float(x.get("price", 0)), reverse=True)
        else:
            result.sort(key=lambda x: str(x.get("name", "")).lower())
        return result
    except Exception as exc:
        raise ValueError(f"ค้นหา/กรอง/เรียงข้อมูลไม่สำเร็จ: {exc}")

def ensure_table_can_order(table):
    if not table:
        raise ValueError("ไม่พบโต๊ะ")
    if table.get("status") == "available":
        raise ValueError("โต๊ะยังไม่เปิดบิล ไม่สามารถรับออเดอร์ได้")
    if table.get("status") == "reserved":
        raise ValueError("โต๊ะนี้ถูกจองไว้")
    if table.get("status") not in {"occupied", "waiting_bill"}:
        raise ValueError("สถานะโต๊ะไม่พร้อมรับออเดอร์")
    return True

def normalize_options(options):
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise ValueError("ตัวเลือกเมนูต้องเป็นข้อมูลแบบ object")
    normalized = {}
    for raw_key, raw_values in options.items():
        key = str(raw_key).strip()
        if not key:
            continue
        if not isinstance(raw_values, list):
            raise ValueError(f"ตัวเลือก {key} ต้องเป็นรายการ")
        values = []
        for raw_value in raw_values:
            if isinstance(raw_value, dict):
                name = clean_text(raw_value.get("name"), "ค่าตัวเลือก", 100)
                price = to_positive_number(raw_value.get("price", 0), "ราคาตัวเลือก", allow_zero=True)
            else:
                name = clean_text(raw_value, "ค่าตัวเลือก", 100)
                price = 0.0
            values.append({"name": name, "price": round(price, 2)})
        normalized[key] = values
    return normalized

def validate_menu_payload(data):
    name = clean_text(data.get("name"), "ชื่อเมนู", 100)
    category = clean_text(data.get("category"), "หมวดหมู่", 60)
    if category not in MENU_CATEGORIES:
        raise ValueError("หมวดเมนูต้องเป็น อาหาร, ของหวาน หรือ เครื่องดื่ม")
    name = validate_person_name(name, "ชื่อเมนู")
    price = to_positive_number(data.get("price"), "ราคา")
    options = normalize_options(data.get("options"))
    image_url = str(data.get("image_url", "")).strip()
    # The Admin form uploads a local file first; the API then stores only the
    # resulting Firebase Storage reference. Do not accept pasted image URLs or
    # base64 data URLs as menu images.
    if image_url and not (image_url.startswith("/uploads/") or image_url.startswith("https://firebasestorage.googleapis.com/")):
        raise ValueError("รูปเมนูต้องอัปโหลดเป็นไฟล์ผ่าน Firebase Storage เท่านั้น")
    return {
        "name": name, "category": category, "price": price,
        "image_url": image_url,
        "is_out_of_stock": bool(data.get("is_out_of_stock", False)),
        "options": options
    }


ACTIVE_RESERVATION_STATUSES = {"waiting", "confirmed", "seated"}


def parse_reservation_dt(value):
    """Parse today's reservation time in 24-hour or common 12-hour formats."""
    text = str(value).strip() if value is not None else ""
    try:
        return datetime.fromisoformat(text)
    except (ValueError, TypeError):
        pass
    normalized = " ".join(text.replace(".", ":").split()).upper()
    for pattern in ("%H:%M", "%I:%M%p", "%I:%M %p"):
        try:
            return datetime.combine(datetime.now().date(), datetime.strptime(normalized, pattern).time())
        except ValueError:
            continue
    raise ValueError("รูปแบบเวลาไม่ถูกต้อง")

def find_reservation_conflict(reservations, table_number, when, ignore_id=None, slot_minutes=90):
    """Return the existing active reservation that overlaps `when` for the same table, or None."""
    window = timedelta(minutes=slot_minutes)
    for res in reservations:
        if not isinstance(res, dict) or res.get("id") == ignore_id:
            continue
        if str(res.get("table_number")) != str(table_number):
            continue
        if res.get("status", "waiting") not in ACTIVE_RESERVATION_STATUSES:
            continue
        try:
            other = parse_reservation_dt(res.get("datetime"))
        except ValueError:
            continue
        if abs(other - when) < window:
            return res
    return None
