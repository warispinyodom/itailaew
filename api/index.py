import json
from datetime import datetime, timezone, timedelta
import uuid
import mimetypes
import os
import base64
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
import urllib.parse
import urllib.request
import urllib.error

try:
    from .config import (
        RESTAURANT_NAME, RESTAURANT_TAGLINE, POLL_INTERVAL_SECONDS,
        BOOTSTRAP_SECRET, FIREBASE_STORAGE_BUCKET, FIREBASE_DB_URL, is_configured, is_local_mode
    )
    from .firebase import get, put, patch, post, delete, transaction, FirebaseError
    from .auth import (
        firebase_signup, firebase_signin, firebase_refresh, verify_id_token, require_user, require_role,
        safe_email, validate_password, create_profile, get_profile, list_users,
        update_user_role, set_user_active, AuthError
    )
    from .biz_logic import (
        now_iso, clean_text, to_positive_number, to_positive_int, calculate_bill,
        paginate, search_filter_sort, ensure_table_can_order, validate_menu_payload,
        parse_reservation_dt, find_reservation_conflict, validate_person_name, MENU_CATEGORIES
    )
except ImportError:
    # Supports `python api/index.py` from the project root as well as package imports on Vercel.
    from config import (
        RESTAURANT_NAME, RESTAURANT_TAGLINE, POLL_INTERVAL_SECONDS,
        BOOTSTRAP_SECRET, FIREBASE_STORAGE_BUCKET, FIREBASE_DB_URL, is_configured, is_local_mode
    )
    from firebase import get, put, patch, post, delete, transaction, FirebaseError
    from auth import (
        firebase_signup, firebase_signin, firebase_refresh, verify_id_token, require_user, require_role,
        safe_email, validate_password, create_profile, get_profile, list_users,
        update_user_role, set_user_active, AuthError
    )
    from biz_logic import (
        now_iso, clean_text, to_positive_number, to_positive_int, calculate_bill,
        paginate, search_filter_sort, ensure_table_can_order, validate_menu_payload,
        parse_reservation_dt, find_reservation_conflict, validate_person_name, MENU_CATEGORIES
    )

PUBLIC_GETS = {"/api/health", "/api/config"}
DEFAULT_PAYMENT_SETTINGS = {"cash_enabled": True, "bank_enabled": True, "qr_enabled": True, "bank_name": "", "account_name": "", "account_number": "", "qr_image_url": ""}

def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"

def points_discount(items, points_to_use):
    points = int(points_to_use or 0)
    if points < 0 or points % 100 != 0:
        raise ValueError("การใช้แต้มต้องใช้ครั้งละ 100 แต้ม")
    subtotal = calculate_bill(items, 0)["subtotal"]
    discount = (points // 100) * 10
    if discount > subtotal:
        raise ValueError("แต้มที่ใช้ลดเกินยอดอาหาร")
    return points, round(discount, 2), subtotal

def json_body(handler):
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length > 4_000_000:
        raise ValueError("ข้อมูลที่ส่งมามีขนาดใหญ่เกินไป")
    raw = handler.rfile.read(length).decode("utf-8") if length else "{}"
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("ข้อมูลต้องเป็น JSON object")
    return data

def save_uploaded_image(data, auth_token=""):
    raw = data.get("image_data")
    filename = clean_text(str(data.get("filename", "image")), "ชื่อไฟล์", 120)
    if not isinstance(raw, str) or not raw.startswith("data:image/"):
        raise ValueError("ไฟล์รูปไม่ถูกต้อง")
    match = re.match(r"data:image/(png|jpeg|jpg|webp);base64,(.+)", raw, re.S)
    if not match:
        raise ValueError("รองรับเฉพาะไฟล์ PNG, JPG, JPEG หรือ WEBP")
    ext = "jpg" if match.group(1) in {"jpeg", "jpg"} else match.group(1)
    try:
        content = base64.b64decode(match.group(2), validate=True)
    except Exception:
        raise ValueError("ข้อมูลไฟล์รูปไม่ถูกต้อง")
    if len(content) > 2_000_000:
        raise ValueError("ไฟล์รูปต้องมีขนาดไม่เกิน 2 MB")
    safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "-", os.path.splitext(filename)[0]).strip("-") or "menu"
    output_name = f"{safe_stem}_{uuid.uuid4().hex[:10]}.{ext}"
    if is_local_mode():
        upload_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "public", "uploads"))
        os.makedirs(upload_dir, exist_ok=True)
        with open(os.path.join(upload_dir, output_name), "wb") as handle:
            handle.write(content)
        return f"/uploads/{output_name}"
    headers = {"Content-Type": f"image/{'jpeg' if ext == 'jpg' else ext}"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    # Vercel is ephemeral, so production images must go to Firebase Storage.
    # Prefer the explicit Vercel variable, then support both Firebase bucket
    # naming formats when it was not configured yet.
    buckets = []
    if FIREBASE_STORAGE_BUCKET:
        buckets.append(FIREBASE_STORAGE_BUCKET)
    match = re.match(r"https://([^.]+)-default-rtdb(?:-[^.]+)?\.", FIREBASE_DB_URL or "")
    if match:
        project_id = match.group(1)
        for candidate in (f"{project_id}.firebasestorage.app", f"{project_id}.appspot.com"):
            if candidate not in buckets:
                buckets.append(candidate)
    if not buckets:
        raise FirebaseError("ยังไม่ได้ตั้งค่า FIREBASE_STORAGE_BUCKET ใน Vercel Environment Variables")
    storage_name = urllib.parse.quote(f"menu-images/{output_name}", safe="")
    uploaded = None
    last_error = None
    for bucket in buckets:
        url = f"https://firebasestorage.googleapis.com/v0/b/{bucket}/o?uploadType=media&name={storage_name}"
        req = urllib.request.Request(url, data=content, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                uploaded = json.loads(res.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8")[:300]
            except Exception:
                detail = str(exc)
            last_error = f"{bucket}: HTTP {exc.code} {detail}"
        except Exception as exc:
            last_error = f"{bucket}: {exc}"
    if not uploaded:
        raise FirebaseError(f"อัปโหลดรูปไป Firebase Storage ไม่สำเร็จ กรุณาตรวจ Bucket และ Storage Rules: {last_error}")
    bucket = next((b for b in buckets if b in str(uploaded.get("bucket", "")) or b == FIREBASE_STORAGE_BUCKET), buckets[0])
    encoded_name = urllib.parse.quote(uploaded.get("name", f"menu-images/{output_name}"), safe="")
    token = (uploaded.get("downloadTokens") or "").split(",")[0]
    download = f"https://firebasestorage.googleapis.com/v0/b/{bucket}/o/{encoded_name}?alt=media"
    if token:
        download += f"&token={urllib.parse.quote(token)}"
    return download

def response(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
    handler.end_headers()
    handler.wfile.write(body)

def error_response(handler, status, message):
    response(handler, status, {"ok": False, "message": str(message)})

def notify_staff(kind, title, detail, target_id=""):
    try:
        nid = new_id("notice")
        put(f"notifications/{nid}", {"id": nid, "kind": kind, "title": title, "detail": detail, "target_id": target_id, "read": False, "created_at": now_iso()})
    except Exception:
        pass

def audit(profile, action, target_type, target_id, detail=""):
    try:
        entry = {
            "id": new_id("log"), "user_id": profile.get("id"), "user_name": profile.get("name"),
            "role": profile.get("role"), "action": action, "target_type": target_type,
            "target_id": target_id, "detail": detail, "timestamp": now_iso()
        }
        post("audit_logs", entry)
    except Exception:
        pass

def find_by_id(collection, item_id):
    data = get(collection) or {}
    if isinstance(data, dict):
        item = data.get(item_id)
        if item:
            return item
        for value in data.values():
            if isinstance(value, dict) and value.get("id") == item_id:
                return value
    return None

def find_table_by_number(table_number):
    tables = get("tables") or {}
    if isinstance(tables, dict):
        for value in tables.values():
            if isinstance(value, dict) and str(value.get("table_number")) == str(table_number).strip():
                return value
    return None

def table_members(table):
    if not table:
        return []
    members = table.get("occupant_ids")
    if isinstance(members, list):
        return [str(x) for x in members if x]
    return [str(table.get("claimed_by"))] if table.get("claimed_by") else []

def table_has_member(table, uid):
    return str(uid) in table_members(table)
def move_active_orders(old_table_id, new_table_id, old_number, new_number):
    orders = get("orders") or {}
    moved_ids = []
    for order in (orders.values() if isinstance(orders, dict) else []):
        if not isinstance(order, dict) or order.get("table_id") != old_table_id:
            continue
        if order.get("status") in ("closed", "merged", "cancelled"):
            continue
        order_id = order.get("id")
        if not order_id:
            continue
        label = f"ย้ายจากโต๊ะ {old_number} → โต๊ะ {new_number}"
        patch(f"orders/{order_id}", {"table_id": new_table_id, "table_number": new_number, "moved_from_table": old_number, "moved_to_table": new_number, "moved_at": now_iso(), "move_label": label})
        moved_ids.append(order_id)
    kitchen = get("kitchen") or {}
    for kid, item in (kitchen.items() if isinstance(kitchen, dict) else []):
        if item.get("order_id") in moved_ids:
            patch(f"kitchen/{kid}", {"table_number": new_number, "moved_from_table": old_number, "moved_to_table": new_number, "moved_at": now_iso(), "move_label": f"ย้ายจากโต๊ะ {old_number} → โต๊ะ {new_number}"})
    return moved_ids
def normalize_available_table(table):
    """Repair stale claims so an available table can be selected."""
    if not table or table.get("status") != "available":
        return table
    members = table_members(table)
    if members:
        repaired = {"status": "occupied", "claimed_by": table.get("claimed_by") or members[0], "occupant_ids": members}
        patch(f"tables/{table['id']}", repaired)
        return {**table, **repaired}
    if not table.get("claimed_by") and not table.get("current_order_id"):
        if table.get("party_size") is not None or table.get("occupant_ids"):
            cleaned = {"party_size": None, "occupant_ids": [], "split_requested": False}
            patch(f"tables/{table['id']}", cleaned)
            return {**table, **cleaned}
        return table
    order = find_by_id("orders", table.get("current_order_id")) if table.get("current_order_id") else None
    if order and order.get("status") not in ("closed", "merged", "cancelled"):
        patch(f"tables/{table['id']}", {"status": "occupied"})
        return {**table, "status": "occupied"}
    patch(f"tables/{table['id']}", {"claimed_by": None, "current_order_id": None, "current_round_id": None, "occupant_ids": [], "party_size": None})
    return {**table, "claimed_by": None, "current_order_id": None, "current_round_id": None, "occupant_ids": [], "party_size": None}

def leave_customer_tables(uid):
    """Cancel only the leaving customer's bill/items; cancel all remaining bills when table is empty."""
    uid = str(uid)
    tables = get("tables") or {}
    orders = get("orders") or {}
    changed = []
    for table in tables.values() if isinstance(tables, dict) else []:
        if not isinstance(table, dict):
            continue
        members = table_members(table)
        if uid not in members:
            continue
        remaining = [member for member in members if member != uid]
        table_id = table.get("id")
        active = [o for o in (orders.values() if isinstance(orders, dict) else []) if o.get("table_id") == table_id and o.get("status") not in {"closed", "merged", "cancelled"}]
        for order in active:
            items = list(order.get("items") or [])
            kept = [item for item in items if str(item.get("customer_id")) != uid]
            removed = len(kept) != len(items) or str(order.get("customer_id")) == uid
            if not removed:
                continue
            if kept:
                next_owner = str(kept[0].get("customer_id") or (remaining[0] if remaining else "")) or None
                patch(f"orders/{order.get('id')}", {"items": kept, "customer_id": next_owner, **calculate_bill(kept, order.get("discount", 0))})
            else:
                patch(f"orders/{order.get('id')}", {"status": "cancelled", "cancelled_at": now_iso(), "cancelled_reason": "Customer ออกจากระบบ"})
                post("audit_logs", {"id": new_id("log"), "user_id": uid, "action": "CANCEL_BILL_ON_LOGOUT", "target_type": "order", "target_id": order.get("id"), "detail": "ยกเลิกบิลของ Customer ที่ Logout", "timestamp": now_iso()})
        if remaining:
            new_owner = remaining[0] if str(table.get("claimed_by")) == uid else table.get("claimed_by")
            seen = table.get("member_last_seen") if isinstance(table.get("member_last_seen"), dict) else {}
            seen.pop(uid, None)
            patch(f"tables/{table_id}", {"occupant_ids": remaining, "claimed_by": new_owner, "member_last_seen": seen})
            changed.append({"table_id": table_id, "new_owner": new_owner, "remaining": remaining})
        else:
            for order in active:
                fresh = find_by_id("orders", order.get("id")) or order
                if fresh.get("status") not in {"closed", "merged", "cancelled"}:
                    patch(f"orders/{order.get('id')}", {"status": "cancelled", "cancelled_at": now_iso(), "cancelled_reason": "ไม่มี Customer เหลือในโต๊ะ"})
            patch(f"tables/{table_id}", {"status": "available", "current_order_id": None, "current_round_id": None, "claimed_by": None, "occupant_ids": [], "member_last_seen": {}, "party_size": None, "split_requested": False})
            post("audit_logs", {"id": new_id("log"), "user_id": uid, "action": "CANCEL_EMPTY_TABLE_BILLS", "target_type": "table", "target_id": table_id, "detail": "ยกเลิกบิลทั้งหมดเพราะไม่มี Customer เหลือ", "timestamp": now_iso()})
            changed.append({"table_id": table_id, "new_owner": None, "remaining": []})
    return changed
def repair_stale_tables():
    tables = get("tables") or {}
    if not isinstance(tables, dict):
        return
    orders = get("orders") or {}
    for table in tables.values():
        if not isinstance(table, dict) or table.get("status") not in {"occupied", "waiting_bill"}:
            continue
        members = table_members(table)
        active = [o for o in (orders.values() if isinstance(orders, dict) else []) if o.get("table_id") == table.get("id") and o.get("status") not in {"closed", "merged", "cancelled"}]
        presence = table.get("member_last_seen") if isinstance(table.get("member_last_seen"), dict) else {}
        stale_members = []
        for member in members:
            seen = presence.get(member)
            try:
                seen_at = datetime.fromisoformat(str(seen).replace("Z", "+00:00")) if seen else None
                # Every new claim writes member_last_seen immediately. A
                # missing/invalid timestamp therefore means legacy or stale
                # membership; do not keep a table occupied just because an
                # old open order still points at it.
                if not seen_at or datetime.now(timezone.utc) - seen_at > timedelta(seconds=90):
                    stale_members.append(member)
            except (TypeError, ValueError):
                stale_members.append(member)
        if stale_members:
            # Reuse the same path as an explicit Logout: remove the stale
            # member's items, cancel its empty bill, update the remaining
            # members, and release the table when nobody remains.
            for member in stale_members:
                leave_customer_tables(member)
            continue
        current_order = find_by_id("orders", table.get("current_order_id")) if table.get("current_order_id") else None
        current_is_inactive = bool(table.get("current_order_id")) and (not current_order or current_order.get("status") in {"closed", "merged", "cancelled"})
        if current_is_inactive and not active:
            table_id = table.get("id")
            patch(f"tables/{table_id}", {"status": "available", "current_order_id": None, "current_round_id": None, "claimed_by": None, "occupant_ids": [], "member_last_seen": {}, "party_size": None, "split_requested": False, "split_requested_by": None, "split_requested_at": None})
            post("audit_logs", {"id": new_id("log"), "action": "AUTO_RELEASE_CANCELLED_BILL_TABLE", "target_type": "table", "target_id": table_id, "detail": "คืนโต๊ะว่างเพราะ current bill ถูกยกเลิกหรือปิดแล้ว", "timestamp": now_iso()})
            continue
        # A customer can select a table before placing the first order. Keep
        # that shared table occupied so other clients see the same members.
        if members:
            continue
        # Do not leave a table occupied merely because old membership fields
        # survived after its open bill was cancelled during logout/expiry.
        if not active:
            table_id = table.get("id")
            patch(f"tables/{table_id}", {"status": "available", "current_order_id": None, "current_round_id": None, "claimed_by": None, "occupant_ids": [], "party_size": None, "split_requested": False, "split_requested_by": None, "split_requested_at": None})
            post("audit_logs", {"id": new_id("log"), "action": "AUTO_RELEASE_STALE_TABLE", "target_type": "table", "target_id": table_id, "detail": "คืนโต๊ะว่างเพราะไม่มีบิลที่ยังเปิดอยู่", "timestamp": now_iso()})
            continue
        for order in active:
            patch(f"orders/{order.get('id')}", {"status": "cancelled", "cancelled_at": now_iso(), "cancelled_reason": "ไม่พบลูกค้าในโต๊ะ"})
        patch(f"tables/{table.get('id')}", {"status": "available", "current_order_id": None, "current_round_id": None, "claimed_by": None, "occupant_ids": [], "party_size": None, "split_requested": False})
        post("audit_logs", {"id": new_id("log"), "action": "AUTO_CANCEL_EMPTY_TABLE", "target_type": "table", "target_id": table.get("id"), "detail": "ยกเลิกบิลเปิดเพราะไม่มี Customer", "timestamp": now_iso()})

def require_staff(profile):
    require_role(profile, "admin", "staff")
def expire_overdue_reservations():
    reservations = get("reservations") or {}
    if not isinstance(reservations, dict):
        return
    now = datetime.now()
    for res in reservations.values():
        if not isinstance(res, dict) or res.get("status") not in {"waiting", "confirmed"}:
            continue
        try:
            when = parse_reservation_dt(res.get("datetime"))
        except ValueError:
            continue
        if now <= when + timedelta(minutes=15):
            continue
        patch(f"reservations/{res.get('id')}", {"status": "expired", "expired_at": now_iso()})
        table = find_table_by_number(res.get("table_number"))
        if table and not table.get("current_order_id") and table.get("status") != "available":
            patch(f"tables/{table['id']}", {"status": "available", "claimed_by": None, "current_order_id": None})

def build_order_items(raw_items):
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("ออเดอร์ต้องมีรายการอาหาร")
    final_items = []
    for raw in raw_items:
        menu = find_by_id("menus", raw.get("menu_id"))
        if not menu:
            raise ValueError("ไม่พบเมนู")
        if menu.get("is_out_of_stock"):
            raise ValueError(f"เมนู {menu.get('name')} หมด")
        qty = to_positive_int(raw.get("quantity", 1), "จำนวน")
        selected_options = raw.get("options") or {}
        if not isinstance(selected_options, dict):
            raise ValueError("ตัวเลือกเมนูไม่ถูกต้อง")
        extra_price = 0.0
        normalized_selected = {}
        for group, selected in selected_options.items():
            if group == "note":
                normalized_selected[group] = str(selected).strip()[:300]
                continue
            definitions = (menu.get("options") or {}).get(group, [])
            if not isinstance(definitions, list):
                continue
            found = None
            for definition in definitions:
                if isinstance(definition, dict):
                    if str(definition.get("name")) == str(selected):
                        found = definition
                        break
                elif str(definition) == str(selected):
                    found = {"name": definition, "price": 0}
                    break
            if found is None:
                raise ValueError(f"ตัวเลือก {group} ไม่ถูกต้อง")
            normalized_selected[group] = found.get("name")
            extra_price += float(found.get("price", 0) or 0)
        final_items.append({
            "menu_id": menu["id"], "name": menu["name"], "unit_price": round(float(menu["price"]) + extra_price, 2),
            "base_price": float(menu["price"]), "quantity": qty, "options": normalized_selected
        })
    return final_items

def public_menu_list():
    data = get("menus") or {}
    legacy_food = {"Pasta", "Pizza", "Steak", "Salad", "อาหาร"}
    result = []
    for value in data.values() if isinstance(data, dict) else []:
        if not isinstance(value, dict):
            continue
        item = dict(value)
        if item.get("category") in legacy_food:
            item["category"] = "อาหาร"
        result.append(item)
    return result

class handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_OPTIONS(self):
        response(self, 204, {})

    def serve_static(self, path):
        try:
            public_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "public"))
            clean_path = path.lstrip("/") or "index.html"
            if ".." in clean_path.split("/"):
                return error_response(self, 400, "เส้นทางไฟล์ไม่ถูกต้อง")
            file_path = os.path.abspath(os.path.join(public_dir, clean_path))
            if not file_path.startswith(public_dir + os.sep):
                return error_response(self, 403, "ไม่อนุญาตให้เข้าถึงไฟล์นี้")
            if not os.path.isfile(file_path):
                file_path = os.path.join(public_dir, "index.html")
            with open(file_path, "rb") as handle:
                body = handle.read()
            content_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"} else ""))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            return error_response(self, 404, "ไม่พบหน้าเว็บ")

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            # Local server also serves the SPA so `python api/index.py` is a one-command run.
            if not path.startswith("/api/"):
                return self.serve_static(path)
            if path == "/api/health":
                return response(self, 200, {"ok": True, "restaurant": RESTAURANT_NAME, "database_configured": is_configured()})
            if path == "/api/config":
                return response(self, 200, {"ok": True, "restaurant": RESTAURANT_NAME, "tagline": RESTAURANT_TAGLINE, "poll_interval": POLL_INTERVAL_SECONDS})

            profile, _ = require_user(self.headers)

            if path == "/api/payment-settings":
                require_role(profile, "admin", "staff")
                settings = get("payment_settings") or {}
                return response(self, 200, {"ok": True, "settings": {**DEFAULT_PAYMENT_SETTINGS, **settings}})

            if path == "/api/me":
                return response(self, 200, {"ok": True, "user": profile})

            if path == "/api/recommended-menus":
                orders = get("orders") or {}
                counts = {}
                if isinstance(orders, dict):
                    for order in orders.values():
                        if not isinstance(order, dict):
                            continue
                        for item in order.get("items", []):
                            menu_id = item.get("menu_id")
                            if menu_id:
                                counts[menu_id] = counts.get(menu_id, 0) + int(item.get("quantity", 0) or 0)
                menus = []
                for menu in public_menu_list():
                    if menu.get("id") in counts:
                        menus.append({**menu, "ordered_count": counts[menu["id"]]})
                menus.sort(key=lambda item: (-item["ordered_count"], str(item.get("name", ""))))
                return response(self, 200, {"ok": True, "items": menus[:3]})

            if path == "/api/menus":
                query = qs.get("q", [""])[0]
                category = qs.get("category", [""])[0]
                sort = qs.get("sort", ["name"])[0]
                page = qs.get("page", ["1"])[0]
                page_size = qs.get("page_size", ["8"])[0]
                menus = search_filter_sort(public_menu_list(), query, category, sort)
                return response(self, 200, {"ok": True, **paginate(menus, page, page_size)})

            if path == "/api/customer/tables":
                require_role(profile, "customer")
                expire_overdue_reservations()
                repair_stale_tables()
                tables = get("tables") or {}
                values = []
                for raw in (list(tables.values()) if isinstance(tables, dict) else []):
                    t = normalize_available_table(raw)
                    members = table_members(t)
                    limit = int(t.get("party_size") or 0)
                    values.append({**t, "occupant_ids": members, "occupant_count": len(members), "remaining_people": max(0, limit-len(members)) if limit else 0, "joinable": bool(limit and len(members) < limit and t.get("status") not in ("available", "reserved"))})
                return response(self, 200, {"ok": True, "tables": values})
            if path == "/api/tables":
                require_staff(profile)
                expire_overdue_reservations()
                repair_stale_tables()
                tables = get("tables") or {}
                values = [normalize_available_table(t) for t in tables.values()] if isinstance(tables, dict) else []
                return response(self, 200, {"ok": True, "tables": values})

            if path == "/api/reservations":
                expire_overdue_reservations()
                reservations = get("reservations") or {}
                values = list(reservations.values()) if isinstance(reservations, dict) else []
                if profile.get("role") == "customer":
                    values = [r for r in values if r.get("customer_id") == profile.get("id")]
                return response(self, 200, {"ok": True, "reservations": values})

            if path.startswith("/api/orders/") and path.endswith("/bill"):
                order_for_bill = find_by_id("orders", path.split("/")[-2])
                if profile.get("role") == "customer":
                    bill_table = find_by_id("tables", order_for_bill.get("table_id")) if order_for_bill else None
                    allowed_customer = order_for_bill and (order_for_bill.get("customer_id") == profile.get("id") or (bill_table and table_has_member(bill_table, profile.get("id"))))
                    if not allowed_customer:
                        return error_response(self, 404, "ไม่พบบิลของคุณ")
                else:
                    require_staff(profile)
                order = find_by_id("orders", path.split("/")[-2])
                if not order or order.get("status") in ("closed", "merged", "cancelled"):
                    return error_response(self, 404, "ไม่พบออเดอร์หรือบิลถูกปิดหรือยกเลิกแล้ว")
                discount = to_positive_number(qs.get("discount", ["0"])[0] or 0, "ส่วนลด", allow_zero=True)
                requested_points = int(order.get("points_to_use", 0) or 0)
                _, points_reduction, subtotal = points_discount(order.get("items", []), requested_points)
                if discount + points_reduction > subtotal:
                    raise ValueError("ส่วนลดรวมเกินยอดอาหาร")
                bill = calculate_bill(order.get("items", []), discount + points_reduction)
                bill["manual_discount"] = round(discount, 2)
                bill["points_used"] = requested_points
                bill["points_discount"] = points_reduction
                customer_points = 0
                points_owner = order.get("points_customer_id") or order.get("customer_id")
                if points_owner:
                    customer_points = int((get_profile(points_owner) or {}).get("member_points", 0) or 0)
                return response(self, 200, {"ok": True, "order": order, "bill": bill, "customer_points": customer_points})

            if path == "/api/customer/table-move-requests":
                require_role(profile, "customer")
                requests = get("table_move_requests") or {}
                values = [r for r in (requests.values() if isinstance(requests, dict) else []) if r.get("customer_id") == profile.get("id")]
                values.sort(key=lambda x: x.get("created_at", ""), reverse=True)
                return response(self, 200, {"ok": True, "requests": values})
            if path == "/api/notifications":
                require_staff(profile)
                notices = get("notifications") or {}
                values = list(notices.values()) if isinstance(notices, dict) else []
                values.sort(key=lambda x: x.get("created_at", ""), reverse=True)
                return response(self, 200, {"ok": True, "notifications": values[:50]})
            if path == "/api/table-move-requests":
                require_staff(profile)
                requests = get("table_move_requests") or {}
                values = list(requests.values()) if isinstance(requests, dict) else []
                values.sort(key=lambda x: x.get("created_at", ""), reverse=True)
                return response(self, 200, {"ok": True, "requests": values})
            if path == "/api/orders":
                orders = get("orders") or {}
                values = list(orders.values()) if isinstance(orders, dict) else []
                if profile.get("role") == "customer":
                    requested_table_id = qs.get("table_id", [""])[0]
                    if requested_table_id:
                        table = find_by_id("tables", requested_table_id)
                        if not table or not table_has_member(table, profile.get("id")):
                            return error_response(self, 403, "คุณไม่มีสิทธิ์ดูออเดอร์ของโต๊ะนี้")
                        current_round = table.get("current_round_id") or table.get("current_order_id")
                        values = [o for o in values if o.get("table_id") == requested_table_id and (not current_round or o.get("round_id") == current_round or o.get("id") == current_round or o.get("split_from") == current_round)]
                    else:
                        values = [o for o in values if o.get("customer_id") == profile.get("id")]
                    kitchen = get("kitchen") or {}
                    kitchen_values = list(kitchen.values()) if isinstance(kitchen, dict) else []
                    for order in values:
                        statuses = {}
                        for item in kitchen_values:
                            if item.get("order_id") != order.get("id"):
                                continue
                            key = item.get("menu_name")
                            statuses[key] = item.get("status", "pending")
                        for order_item in order.get("items", []):
                            order_item["kitchen_status"] = statuses.get(order_item.get("name"), "pending")
                if profile.get("role") in ("admin", "staff"):
                    for order in values:
                        table = find_by_id("tables", order.get("table_id"))
                        order["split_requested"] = bool(table and table.get("split_requested"))
                return response(self, 200, {"ok": True, "orders": values})

            if path == "/api/kitchen":
                require_staff(profile)
                kitchen = get("kitchen") or {}
                values = list(kitchen.values()) if isinstance(kitchen, dict) else []
                orders = get("orders") or {}
                order_map = orders if isinstance(orders, dict) else {}
                for item in values:
                    order = order_map.get(item.get("order_id")) if isinstance(order_map, dict) else None
                    if isinstance(order, dict):
                        item["table_number"] = order.get("table_number", item.get("table_number"))
                        for key in ("moved_from_table", "moved_to_table", "moved_at", "move_label"):
                            if order.get(key) is not None:
                                item[key] = order[key]
                values.sort(key=lambda x: x.get("timestamp", ""))
                return response(self, 200, {"ok": True, "items": values})

            if path == "/api/users":
                require_role(profile, "admin")
                return response(self, 200, {"ok": True, "users": list_users()})

            if path == "/api/audit-logs":
                require_role(profile, "admin")
                logs = get("audit_logs") or {}
                values = list(logs.values()) if isinstance(logs, dict) else []
                values.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
                return response(self, 200, {"ok": True, "logs": values})

            if path == "/api/dashboard":
                require_role(profile, "admin")
                orders = get("orders") or {}
                values = [o for o in orders.values() if isinstance(o, dict)] if isinstance(orders, dict) else []
                closed = [o for o in values if o.get("status") == "closed"]
                total_sales = round(sum(float(o.get("total", 0)) for o in closed), 2)
                today = now_iso()[:10]
                today_sales = round(sum(float(o.get("total", 0)) for o in closed if str(o.get("closed_at", "")).startswith(today)), 2)
                sold = {}
                for order in closed:
                    for item in order.get("items", []):
                        name = item.get("name", "Unknown")
                        sold[name] = sold.get(name, 0) + int(item.get("quantity", 0))
                best = sorted([{"name": k, "quantity": v} for k, v in sold.items()], key=lambda x: x["quantity"], reverse=True)[:8]
                # Daily sales for the last 7 days (oldest -> newest)
                daily = {}
                for i in range(6, -1, -1):
                    daily[(datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")] = 0.0
                for o in closed:
                    day = str(o.get("closed_at", ""))[:10]
                    if day in daily:
                        daily[day] = round(daily[day] + float(o.get("total", 0)), 2)
                sales_7d = [{"date": k, "total": v} for k, v in daily.items()]
                open_orders = len([o for o in values if o.get("status") not in ("closed", "merged")])
                avg_bill = round(total_sales / len(closed), 2) if closed else 0.0
                # Tables by status
                tables = get("tables") or {}
                table_values = [t for t in tables.values() if isinstance(t, dict)] if isinstance(tables, dict) else []
                table_status = {"available": 0, "occupied": 0, "waiting_bill": 0, "reserved": 0}
                for t in table_values:
                    st = t.get("status", "available")
                    table_status[st] = table_status.get(st, 0) + 1
                # Reservations
                reservations = get("reservations") or {}
                res_values = [r for r in reservations.values() if isinstance(r, dict)] if isinstance(reservations, dict) else []
                res_status = {}
                for r in res_values:
                    st = r.get("status", "waiting")
                    res_status[st] = res_status.get(st, 0) + 1
                res_today = len([r for r in res_values if str(r.get("datetime", "")).startswith(today)])
                # Users by role
                users = list_users()
                user_roles = {"admin": 0, "staff": 0, "customer": 0}
                for u in users:
                    user_roles[u.get("role", "customer")] = user_roles.get(u.get("role", "customer"), 0) + 1
                inactive_users = len([u for u in users if u.get("active") is False])
                return response(self, 200, {
                    "ok": True, "today_sales": today_sales, "total_sales": total_sales,
                    "closed_orders": len(closed), "best_sellers": best,
                    "open_orders": open_orders, "avg_bill": avg_bill, "sales_7d": sales_7d,
                    "tables": {"total": len(table_values), **table_status},
                    "reservations": {"total": len(res_values), "today": res_today, "by_status": res_status},
                    "users": {"total": len(users), "inactive": inactive_users, **user_roles},
                })

            return error_response(self, 404, "ไม่พบ API ที่ร้องขอ")
        except AuthError as exc:
            return error_response(self, 403, str(exc))
        except ValueError as exc:
            return error_response(self, 400, str(exc))
        except Exception as exc:
            return error_response(self, 500, "เกิดข้อผิดพลาดภายในระบบ กรุณาลองใหม่")

    def do_POST(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            data = json_body(self)

            if path == "/api/auth/register":
                email = str(data.get("email", "")).strip().lower()
                password = data.get("password")
                name = str(data.get("name", "")).strip()
                if not safe_email(email):
                    raise ValueError("อีเมลไม่ถูกต้อง")
                if not validate_password(password):
                    raise ValueError("รหัสผ่านต้องมีอย่างน้อย 6 ตัวอักษร")
                validate_person_name(name)

                auth = firebase_signup(email, password)
                profile = create_profile(auth["localId"], email, name, "customer", password=password)
                return response(self, 201, {"ok": True, "message": "สมัครสมาชิกสำเร็จ", "token": auth["idToken"], "refresh_token": auth.get("refreshToken") or auth.get("idToken"), "user": profile})

            if path == "/api/auth/login":
                email = str(data.get("email", "")).strip().lower()
                password = data.get("password")
                if not safe_email(email) or not isinstance(password, str):
                    raise ValueError("กรุณากรอกอีเมลและรหัสผ่าน")
                auth = firebase_signin(email, password)
                profile = get_profile(auth["localId"])
                return response(self, 200, {"ok": True, "message": "เข้าสู่ระบบสำเร็จ", "token": auth["idToken"], "refresh_token": auth.get("refreshToken") or auth.get("idToken"), "user": profile})

            if path == "/api/auth/refresh":
                refreshed = firebase_refresh(data.get("refresh_token"))
                uid = refreshed.get("user_id") or refreshed.get("localId")
                profile = get_profile(uid)
                return response(self, 200, {"ok": True, "token": refreshed.get("id_token") or refreshed.get("idToken"), "refresh_token": refreshed.get("refresh_token") or data.get("refresh_token"), "user": profile})

            if path == "/api/bootstrap":
                if not BOOTSTRAP_SECRET or data.get("secret") != BOOTSTRAP_SECRET:
                    return error_response(self, 403, "Bootstrap secret ไม่ถูกต้อง")
                email = str(data.get("email", "")).strip().lower()
                password = data.get("password")
                name = str(data.get("name", "itailaew Admin")).strip()
                if not safe_email(email) or not validate_password(password):
                    raise ValueError("ข้อมูล Admin ไม่ถูกต้อง")
                auth = firebase_signup(email, password)
                profile = create_profile(auth["localId"], email, name, "admin", password=password)
                return response(self, 201, {"ok": True, "message": "สร้าง Admin สำเร็จ", "user": profile})

            profile, auth_token = require_user(self.headers)

            if path == "/api/uploads/menu-image":
                require_role(profile, "admin")
                image_url = save_uploaded_image(data, auth_token)
                return response(self, 201, {"ok": True, "image_url": image_url})
            if path == "/api/menus":
                require_role(profile, "admin")
                menu = validate_menu_payload(data)
                menu["id"] = new_id("menu")
                menu["created_at"] = now_iso()
                put(f"menus/{menu['id']}", menu)
                audit(profile, "CREATE_MENU", "menu", menu["id"], menu["name"])
                return response(self, 201, {"ok": True, "menu": menu})

            if path == "/api/users/staff":
                require_role(profile, "admin")
                email = str(data.get("email", "")).strip().lower()
                password = data.get("password")
                name = str(data.get("name", "")).strip()
                if not safe_email(email) or not validate_password(password):
                    raise ValueError("ข้อมูล Staff ไม่ถูกต้อง")
                validate_person_name(name, "ชื่อ Staff")
                auth = firebase_signup(email, password)
                staff = create_profile(auth["localId"], email, name, "staff", password=password)
                audit(profile, "CREATE_STAFF", "user", staff["id"], email)
                return response(self, 201, {"ok": True, "user": staff})

            if path == "/api/orders":
                require_staff(profile)
                table_id = clean_text(data.get("table_id"), "โต๊ะ", 50)
                table = find_by_id("tables", table_id)
                if not table:
                    raise ValueError("ไม่พบโต๊ะ")
                if table.get("status") == "reserved":
                    raise ValueError("โต๊ะนี้ถูกตั้งสถานะจองไว้ กรุณาจัดลูกค้าเข้านั่งก่อน")
                final_items = build_order_items(data.get("items"))
                existing = find_by_id("orders", table["current_order_id"]) if table.get("current_order_id") else None
                if existing and existing.get("status") not in ("closed", "merged"):
                    # Table already has an open bill: add the new items to it
                    order_id = existing["id"]
                    all_items = existing.get("items", []) + final_items
                    bill = calculate_bill(all_items, existing.get("discount", 0))
                    patch(f"orders/{order_id}", {"items": all_items, **bill})
                    order = {**existing, "items": all_items, **bill}
                else:
                    order_id = new_id("order")
                    bill = calculate_bill(final_items, 0)
                    order = {
                        "id": order_id, "table_id": table_id, "table_number": table.get("table_number"),
                        "customer_id": data.get("customer_id"), "items": final_items, **bill,
                        "status": "open", "created_by": profile["id"], "created_at": now_iso(), "round_id": order_id
                    }
                    put(f"orders/{order_id}", order)
                    patch(f"tables/{table_id}", {"status": "occupied", "current_order_id": order_id, "current_round_id": order_id})
                for item in final_items:
                    kid = new_id("kit")
                    put(f"kitchen/{kid}", {
                        "id": kid, "order_id": order_id, "order_item_id": f"{order_id}_{item['menu_id']}",
                        "menu_name": item["name"], "options": item["options"],
                        "table_number": table.get("table_number"), "quantity": item["quantity"],
                        "status": "pending", "timestamp": now_iso()
                    })
                audit(profile, "CREATE_ORDER", "order", order_id, f"table={table_id}")
                notify_staff("new_order", "มีออเดอร์ใหม่", f"โต๊ะ {table.get('table_number')}", order_id)
                return response(self, 201, {"ok": True, "order": order})

            if path == "/api/orders/split-by-customer":
                require_staff(profile)
                order_id = clean_text(data.get("order_id"), "Order ID", 80)
                order = find_by_id("orders", order_id)
                if not order or order.get("status") in ("closed", "merged", "cancelled"):
                    raise ValueError("ไม่พบออเดอร์หรือบิลถูกปิดแล้ว")
                table = find_by_id("tables", order.get("table_id"))
                if not table or not table.get("split_requested"):
                    raise ValueError("ลูกค้ายังไม่ได้ขอแยกบิล")
                items = order.get("items", [])
                groups = {}
                central = []
                for item in items:
                    cid = item.get("customer_id")
                    (groups.setdefault(cid, []) if cid else central).append(item)
                created = []
                for cid, group in groups.items():
                    if not cid or not group:
                        continue
                    nid = new_id("order")
                    bill = calculate_bill(group, 0)
                    child = {**bill, "id": nid, "table_id": order["table_id"], "table_number": order.get("table_number"), "customer_id": cid, "points_customer_id": cid, "items": group, "status": "open", "created_by": profile["id"], "created_at": now_iso(), "split_from": order_id, "round_id": order.get("round_id") or order_id, "bill_group": "split"}
                    put(f"orders/{nid}", child)
                    created.append(child)
                if central:
                    patch(f"orders/{order_id}", {"items": central, **calculate_bill(central, 0), "split_role": "central", "bill_group": "central"})
                    current_order_id = order_id
                else:
                    patch(f"orders/{order_id}", {"status": "merged", "split_role": "split_parent"})
                    current_order_id = created[0]["id"] if created else None
                patch(f"tables/{order['table_id']}", {"split_requested": False, "split_requested_by": None, "current_order_id": current_order_id, "current_round_id": order.get("round_id") or order_id})
                return response(self, 200, {"ok": True, "orders": created, "central": central})
            if path == "/api/orders/split":
                require_staff(profile)
                order_id = clean_text(data.get("order_id"), "Order ID", 80)
                order = find_by_id("orders", order_id)
                if not order or order.get("status") == "closed":
                    raise ValueError("ไม่พบออเดอร์หรือบิลถูกปิดแล้ว")
                selected_ids = data.get("item_indexes")
                new_table_id = clean_text(data.get("new_table_id"), "โต๊ะใหม่", 50)
                new_table = find_by_id("tables", new_table_id)
                if not isinstance(selected_ids, list) or not selected_ids:
                    raise ValueError("กรุณาเลือกรายการที่ต้องการแยก")
                if not new_table or new_table.get("status") != "available":
                    raise ValueError("โต๊ะใหม่ต้องว่าง")
                items = order.get("items", [])
                selected = [items[int(i)] for i in selected_ids if 0 <= int(i) < len(items)]
                if not selected:
                    raise ValueError("ไม่พบรายการที่ต้องการแยก")
                remaining = [item for i, item in enumerate(items) if i not in [int(x) for x in selected_ids]]
                new_id_value = new_id("order")
                new_bill = calculate_bill(selected, 0)
                new_order = {**new_bill, "id": new_id_value, "table_id": new_table_id,
                             "table_number": new_table.get("table_number"), "customer_id": order.get("customer_id"),
                             "items": selected, "status": "open", "created_by": profile["id"], "created_at": now_iso(),
                             "split_from": order_id, "round_id": order.get("round_id") or order_id}
                put(f"orders/{new_id_value}", new_order)
                old_bill = calculate_bill(remaining, order.get("discount", 0))
                patch(f"orders/{order_id}", {"items": remaining, **old_bill})
                patch(f"tables/{new_table_id}", {"status": "occupied", "current_order_id": new_id_value})
                audit(profile, "SPLIT_BILL", "order", order_id, f"new={new_id_value}")
                return response(self, 200, {"ok": True, "new_order": new_order})

            if path == "/api/customer/tables/claim":
                require_role(profile, "customer")
                table_id = clean_text(data.get("table_id"), "โต๊ะ", 50)
                uid = str(profile.get("id"))
                meta = {"joined": False, "already": False}
                def claim_update(current):
                    if not isinstance(current, dict):
                        raise ValueError("ไม่พบโต๊ะ")
                    members = table_members(current)
                    if uid in members:
                        meta["already"] = True
                        seen = current.get("member_last_seen") if isinstance(current.get("member_last_seen"), dict) else {}
                        seen[uid] = now_iso()
                        return {**current, "occupant_ids": members, "member_last_seen": seen}
                    if current.get("status") == "reserved":
                        raise ValueError("โต๊ะนี้มีการจองไว้ กรุณาเลือกโต๊ะอื่น")
                    if current.get("status") == "available" and not members:
                        return {**current, "status": "occupied", "claimed_by": uid, "occupant_ids": [uid], "member_last_seen": {uid: now_iso()}}
                    limit = int(current.get("party_size") or 0)
                    if current.get("status") in ("occupied", "waiting_bill") and limit and len(members) < limit:
                        meta["joined"] = True
                        seen = current.get("member_last_seen") if isinstance(current.get("member_last_seen"), dict) else {}
                        seen[uid] = now_iso()
                        return {**current, "occupant_ids": members + [uid], "member_last_seen": seen}
                    raise ValueError("โต๊ะนี้เต็มแล้ว กรุณาเลือกโต๊ะอื่น")
                table = transaction(f"tables/{table_id}", claim_update)
                members = table_members(table)
                if not meta["already"]:
                    audit(profile, "CLAIM_TABLE", "table", table_id)
                return response(self, 200, {"ok": True, "table": {**table, "occupant_ids": members}, "claimed": True, "joined": meta["joined"]})
            if path == "/api/customer/tables/confirm":
                require_role(profile, "customer")
                table_id = clean_text(data.get("table_id"), "โต๊ะ", 50)
                table = find_by_id("tables", table_id)
                if not table or not table_has_member(table, profile.get("id")):
                    raise ValueError("คุณยังไม่ได้เลือกโต๊ะนี้")
                capacity = int(table.get("capacity", 4) or 4)
                party_size = to_positive_int(data.get("party_size"), "จำนวนคน")
                if table.get("party_size") and profile.get("id") != table.get("claimed_by"):
                    raise ValueError("โต๊ะนี้กำหนดจำนวนคนโดยลูกค้าคนแรกแล้ว")
                if party_size > capacity:
                    raise ValueError(f"โต๊ะนี้รองรับได้สูงสุด {capacity} คน")
                seen = table.get("member_last_seen") if isinstance(table.get("member_last_seen"), dict) else {}
                seen[str(profile.get("id"))] = now_iso()
                patch(f"tables/{table_id}", {"party_size": party_size, "occupant_ids": table_members(table), "member_last_seen": seen})
                return response(self, 200, {"ok": True, "table": {**table, "party_size": party_size, "capacity": capacity}})
            if path == "/api/customer/leave":
                require_role(profile, "customer")
                changes = leave_customer_tables(profile.get("id"))
                audit(profile, "LEAVE_TABLE", "table", changes[0]["table_id"] if changes else "", "logout/session expired")
                return response(self, 200, {"ok": True, "changes": changes})
            if path == "/api/customer/presence":
                require_role(profile, "customer")
                table_id = clean_text(data.get("table_id"), "โต๊ะ", 50)
                table = find_by_id("tables", table_id)
                if not table or not table_has_member(table, profile.get("id")):
                    raise ValueError("คุณไม่ได้เป็นสมาชิกของโต๊ะนี้")
                seen = table.get("member_last_seen") if isinstance(table.get("member_last_seen"), dict) else {}
                seen[str(profile.get("id"))] = now_iso()
                patch(f"tables/{table_id}", {"member_last_seen": seen, "status": "occupied" if table.get("status") == "available" else table.get("status")})
                return response(self, 200, {"ok": True})
            if path == "/api/customer/tables/release":
                require_role(profile, "customer")
                table_id = clean_text(data.get("table_id"), "โต๊ะ", 50)
                table = find_by_id("tables", table_id)
                if table and table.get("claimed_by") == profile.get("id") and not table.get("current_order_id"):
                    patch(f"tables/{table_id}", {"status": "available", "claimed_by": None, "occupant_ids": [], "party_size": None})
                return response(self, 200, {"ok": True})
            if path == "/api/customer/table-move-requests":
                require_role(profile, "customer")
                tables = get("tables") or {}
                current_table = next((t for t in (tables.values() if isinstance(tables, dict) else []) if t.get("claimed_by") == profile.get("id") and t.get("status") != "available"), None)
                if not current_table:
                    raise ValueError("คุณยังไม่ได้เลือกโต๊ะ")
                orders = get("orders") or {}
                active = [o for o in (orders.values() if isinstance(orders, dict) else []) if o.get("table_id") == current_table.get("id") and o.get("status") not in ("closed", "merged")]
                if not active:
                    raise ValueError("ต้องสั่งอาหารก่อนจึงจะขอย้ายโต๊ะได้")
                requests = get("table_move_requests") or {}
                existing = next((r for r in requests.values() if r.get("customer_id") == profile.get("id") and r.get("status") == "pending"), None) if isinstance(requests, dict) else None
                if existing:
                    return response(self, 200, {"ok": True, "request": existing, "message": "มีคำขอย้ายโต๊ะที่รอ Staff ดำเนินการอยู่แล้ว"})
                order = active[0]
                target_id = clean_text(data.get("new_table_id"), "โต๊ะปลายทาง", 50)
                target = find_by_id("tables", target_id)
                if not target or target.get("status") != "available":
                    raise ValueError("โต๊ะปลายทางไม่ว่าง")
                if target_id == order.get("table_id"):
                    raise ValueError("โต๊ะปลายทางต้องต่างจากโต๊ะปัจจุบัน")
                request = {"id": new_id("move"), "customer_id": profile.get("id"), "customer_name": profile.get("name"), "table_id": order.get("table_id"), "table_number": order.get("table_number"), "new_table_id": target_id, "new_table_number": target.get("table_number"), "status": "pending", "created_at": now_iso()}
                put(f"table_move_requests/{request['id']}", request)
                notify_staff("move_request", "มีคำขอย้ายโต๊ะ", f"โต๊ะ {request.get('table_number')} ไป {request.get('new_table_number')}", request["id"])
                return response(self, 201, {"ok": True, "request": request})
            if path == "/api/customer/split-bill-requests":
                require_role(profile, "customer")
                table_id = clean_text(data.get("table_id"), "โต๊ะ", 50)
                table = find_by_id("tables", table_id)
                if not table or not table_has_member(table, profile.get("id")):
                    raise ValueError("คุณไม่ได้เป็นสมาชิกของโต๊ะนี้")
                orders = get("orders") or {}
                active = [o for o in (orders.values() if isinstance(orders, dict) else []) if o.get("table_id") == table_id and o.get("status") not in ("closed", "merged", "cancelled")]
                if not active:
                    raise ValueError("ยังไม่มีบิลที่เปิดอยู่")
                # The table's first customer owns unassigned items by default.
                default_owner = str(table.get("claimed_by") or (table_members(table) or [profile.get("id")])[0])
                members = table_members(table)
                member_profiles = {}
                for member_id in members:
                    try:
                        member = get_profile(member_id)
                        member_profiles[member_id] = {"id": member_id, "name": member.get("name") or member.get("email") or member_id}
                    except Exception:
                        member_profiles[member_id] = {"id": member_id, "name": member_id}
                assignments = data.get("assignments") or []
                allowed = {str(x) for x in members}
                already_split = any(o.get("bill_group") == "split" for o in active)
                initial_split = not table.get("split_requested") and not already_split
                for order in active:
                    items = list(order.get("items") or [])
                    changed = False
                    for item in items:
                        if initial_split or not item.get("customer_id"):
                            item["customer_id"] = default_owner
                            changed = True
                    for assignment in assignments:
                        try:
                            idx, owner = int(assignment.get("item_index")), str(assignment.get("customer_id"))
                            assignment_order = str(assignment.get("order_id") or order.get("id"))
                        except Exception:
                            continue
                        if assignment_order == str(order.get("id")) and 0 <= idx < len(items) and owner in allowed:
                            items[idx]["customer_id"] = owner
                            changed = True
                    if changed:
                        patch(f"orders/{order['id']}", {"items": items, **calculate_bill(items, order.get("discount", 0))})
                        order["items"] = items
                if not already_split:
                    patch(f"tables/{table_id}", {"split_requested": True, "split_requested_by": profile.get("id"), "split_requested_at": now_iso()})
                    notify_staff("split_request", "มีคำขอแยกบิล", f"โต๊ะ {table.get('table_number')}", table_id)
                message = "อัปเดตเจ้าของรายการแล้ว" if already_split else "ส่งคำขอแยกบิลให้ Staff แล้ว"
                return response(self, 201, {"ok": True, "message": message, "members": members, "member_profiles": member_profiles, "orders": active, "default_owner": default_owner})
            if path == "/api/customer/orders":
                require_role(profile, "customer")
                table_id = clean_text(data.get("table_id"), "โต๊ะ", 50)
                table = find_by_id("tables", table_id)
                if not table:
                    raise ValueError("ไม่พบโต๊ะ")
                if not table_has_member(table, profile.get("id")):
                    raise ValueError("คุณยังไม่ได้เข้าร่วมโต๊ะนี้")
                if table.get("status") == "reserved":
                    raise ValueError("โต๊ะนี้ถูกจองไว้")
                client_request_id = str(data.get("client_request_id") or "").strip()[:120]
                if client_request_id:
                    all_orders = get("orders") or {}
                    duplicate = next((o for o in (all_orders.values() if isinstance(all_orders, dict) else []) if o.get("table_id") == table_id and o.get("client_request_id") == client_request_id), None)
                    if duplicate:
                        return response(self, 200, {"ok": True, "duplicate": True, "order": duplicate})
                capacity = int(table.get("capacity", 4) or 4)
                party_size = to_positive_int(data.get("party_size") or table.get("party_size"), "จำนวนคน")
                if party_size > capacity:
                    raise ValueError(f"โต๊ะนี้รองรับได้สูงสุด {capacity} คน")
                final_items = build_order_items(data.get("items"))
                for item in final_items:
                    item["customer_id"] = profile.get("id")
                existing = find_by_id("orders", table.get("current_order_id")) if table.get("current_order_id") else None
                # A bill waiting for checkout is already a separate billing
                # event. A later customer order must start a new round rather
                # than mutating that bill; only table moves preserve it.
                if existing and existing.get("status") == "open":
                    order_id = existing["id"]
                    all_items = existing.get("items", []) + final_items
                    bill = calculate_bill(all_items, existing.get("discount", 0))
                    patch(f"orders/{order_id}", {"items": all_items, "party_size": table.get("party_size") or party_size, "client_request_id": client_request_id, **bill})
                    round_id = existing.get("round_id") or existing.get("id")
                    if not existing.get("round_id"):
                        patch(f"orders/{order_id}", {"round_id": round_id})
                    patch(f"tables/{table_id}", {"current_round_id": round_id})
                    order = {**existing, "items": all_items, "party_size": table.get("party_size") or party_size, "round_id": round_id, **bill}
                else:
                    order_id = new_id("order")
                    bill = calculate_bill(final_items, 0)
                    order = {"id": order_id, "table_id": table_id, "table_number": table.get("table_number"),
                             "customer_id": profile["id"], "party_size": party_size, "items": final_items, **bill, "status": "open",
                             "created_by": profile["id"], "created_at": now_iso(), "source": "qr", "round_id": order_id, "client_request_id": client_request_id}
                    put(f"orders/{order_id}", order)
                    patch(f"tables/{table_id}", {"status": "occupied", "claimed_by": table.get("claimed_by") or profile["id"], "party_size": table.get("party_size") or party_size, "current_order_id": order_id, "current_round_id": order_id})
                for item in final_items:
                    kid = new_id("kit")
                    put(f"kitchen/{kid}", {"id": kid, "order_id": order_id, "order_item_id": f"{order_id}_{item['menu_id']}",
                         "menu_name": item["name"], "options": item["options"], "table_number": table.get("table_number"),
                         "quantity": item["quantity"], "status": "pending", "timestamp": now_iso()})
                notify_staff("new_order", "มีออเดอร์ใหม่", f"โต๊ะ {table.get('table_number')}", order_id)
                return response(self, 201, {"ok": True, "order": order})
            if path == "/api/tables/move":
                require_staff(profile)
                old_id = clean_text(data.get("old_table_id"), "โต๊ะเดิม", 50)
                new_table_id = clean_text(data.get("new_table_id"), "โต๊ะใหม่", 50)
                old_table = find_by_id("tables", old_id)
                new_table = find_by_id("tables", new_table_id)
                if not old_table or not new_table:
                    raise ValueError("ไม่พบโต๊ะที่ระบุ")
                if old_table.get("status") == "available":
                    raise ValueError("โต๊ะเดิมยังไม่มีออเดอร์")
                if new_table.get("status") != "available":
                    raise ValueError("โต๊ะใหม่ไม่ว่าง")
                order_id = old_table.get("current_order_id")
                members = table_members(old_table)
                presence = old_table.get("member_last_seen") if isinstance(old_table.get("member_last_seen"), dict) else {}
                order = find_by_id("orders", order_id) if order_id else None
                patch(f"tables/{old_id}", {"status": "available", "current_order_id": None, "current_round_id": None, "claimed_by": None, "occupant_ids": [], "member_last_seen": {}, "party_size": None})
                patch(f"tables/{new_table_id}", {"status": "occupied", "current_order_id": order_id, "current_round_id": old_table.get("current_round_id") or (order or {}).get("round_id") or order_id, "claimed_by": old_table.get("claimed_by") or (members[0] if members else None), "occupant_ids": members, "member_last_seen": presence, "party_size": old_table.get("party_size")})
                if order_id:
                    patch(f"orders/{order_id}", {"table_id": new_table_id, "table_number": new_table.get("table_number"), "moved_from_table": old_table.get("table_number"), "moved_to_table": new_table.get("table_number"), "moved_at": now_iso(), "move_label": f"ย้ายจากโต๊ะ {old_table.get('table_number')} → โต๊ะ {new_table.get('table_number')}"})
                    kitchen = get("kitchen") or {}
                    for kid, item in (kitchen.items() if isinstance(kitchen, dict) else []):
                        if item.get("order_id") == order_id:
                            patch(f"kitchen/{kid}", {"table_number": new_table.get("table_number"), "moved_from_table": old_table.get("table_number"), "moved_to_table": new_table.get("table_number"), "moved_at": now_iso(), "move_label": f"ย้ายจากโต๊ะ {old_table.get('table_number')} → โต๊ะ {new_table.get('table_number')}"})
                move_active_orders(old_id, new_table_id, old_table.get("table_number"), new_table.get("table_number"))
                audit(profile, "MOVE_TABLE", "table", old_id, f"to={new_table_id}")
                return response(self, 200, {"ok": True, "message": "ย้ายโต๊ะสำเร็จ"})

            if path == "/api/tables/merge":
                require_staff(profile)
                source_id = clean_text(data.get("source_table_id"), "โต๊ะต้นทาง", 50)
                target_id = clean_text(data.get("target_table_id"), "โต๊ะปลายทาง", 50)
                source = find_by_id("tables", source_id)
                target = find_by_id("tables", target_id)
                if not source or not target:
                    raise ValueError("ไม่พบโต๊ะ")
                if not source.get("current_order_id") or not target.get("current_order_id"):
                    raise ValueError("ต้องมีออเดอร์ทั้งสองโต๊ะก่อนรวมโต๊ะ")
                source_order = find_by_id("orders", source["current_order_id"])
                target_order = find_by_id("orders", target["current_order_id"])
                merged_items = target_order.get("items", []) + source_order.get("items", [])
                bill = calculate_bill(merged_items, target_order.get("discount", 0))
                patch(f"orders/{target_order['id']}", {"items": merged_items, **bill, "merged_table_ids": [source_id, target_id]})
                patch(f"orders/{source_order['id']}", {"status": "merged", "merged_into": target_order["id"]})
                patch(f"tables/{source_id}", {"status": "available", "current_order_id": None})
                audit(profile, "MERGE_TABLE", "table", target_id, f"source={source_id}")
                return response(self, 200, {"ok": True, "message": "รวมโต๊ะสำเร็จ", "order_id": target_order["id"]})

            if path == "/api/reservations":
                expire_overdue_reservations()
                name = validate_person_name(data.get("customer_name"), "ชื่อผู้จอง")
                phone = clean_text(data.get("phone"), "เบอร์โทร", 30)
                if not phone.isdigit() or len(phone) != 10:
                    raise ValueError("เบอร์โทรต้องเป็นตัวเลข 10 หลักเท่านั้น")
                table_number = clean_text(data.get("table_number"), "โต๊ะ", 20)
                when_text = clean_text(data.get("datetime"), "เวลา", 10)
                try:
                    when = parse_reservation_dt(when_text)
                except ValueError:
                    raise ValueError("กรุณาระบุเวลาในรูปแบบ HH:MM")
                if when.date() != datetime.now().date():
                    raise ValueError("ระบบรับจองเฉพาะวันนี้เท่านั้น")
                if when.hour < 9:
                    raise ValueError("ระบบเปิดให้จองตั้งแต่ 09:00 เป็นต้นไป")
                minimum_time = datetime.now() + timedelta(minutes=30)
                if when < minimum_time:
                    raise ValueError("กรุณาจองล่วงหน้าอย่างน้อย 30 นาที")
                table_for_reservation = find_table_by_number(table_number)
                if not table_for_reservation:
                    raise ValueError("ไม่พบโต๊ะที่เลือก")
                by_staff = profile.get("role") in ("admin", "staff")
                party_size_value = data.get("party_size")
                if by_staff and party_size_value in (None, ""):
                    raise ValueError("กรุณาระบุจำนวนคน")
                party_size = to_positive_int(party_size_value, "จำนวนคน") if party_size_value not in (None, "") else None
                capacity = int(table_for_reservation.get("capacity", 4) or 4)
                if party_size is not None and party_size > capacity:
                    raise ValueError(f"โต๊ะ {table_number} รองรับได้สูงสุด {capacity} คน")
                existing_res = get("reservations") or {}
                existing_list = list(existing_res.values()) if isinstance(existing_res, dict) else []
                clash = find_reservation_conflict(existing_list, table_number, when)
                if clash:
                    raise ValueError(f"โต๊ะ {table_number} ถูกจองไว้แล้วในช่วงเวลาใกล้เคียง ({clash.get('datetime')}) กรุณาเลือกเวลาหรือโต๊ะอื่น")
                reservation = {
                    "id": new_id("res"), "customer_id": None if by_staff else profile["id"], "customer_name": name,
                    "phone": phone, "table_number": table_number, "party_size": party_size, "datetime": when_text,
                    # A booking taken by staff (phone / walk-in) is confirmed immediately
                    "status": "confirmed" if by_staff else "waiting",
                    "source": "staff" if by_staff else "customer",
                    "created_by": profile["id"], "created_at": now_iso()
                }
                put(f"reservations/{reservation['id']}", reservation)
                audit(profile, "CREATE_RESERVATION", "reservation", reservation["id"], f"table={table_number} at={when_text}")
                return response(self, 201, {"ok": True, "reservation": reservation})

            return error_response(self, 404, "ไม่พบ API ที่ร้องขอ")
        except AuthError as exc:
            return error_response(self, 403, str(exc))
        except (ValueError, FirebaseError) as exc:
            return error_response(self, 400, str(exc))
        except Exception:
            return error_response(self, 500, "เกิดข้อผิดพลาดภายในระบบ กรุณาลองใหม่")

    def do_PATCH(self):
        try:
            path = urlparse(self.path).path
            data = json_body(self)
            profile, _ = require_user(self.headers)

            if path == "/api/payment-settings":
                require_role(profile, "admin")
                settings = {
                    "cash_enabled": bool(data.get("cash_enabled", True)),
                    "bank_enabled": bool(data.get("bank_enabled", True)),
                    "qr_enabled": bool(data.get("qr_enabled", True)),
                    "bank_name": clean_text(data.get("bank_name", ""), "ชื่อธนาคาร", 120),
                    "account_name": clean_text(data.get("account_name", ""), "ชื่อบัญชี", 120),
                    "account_number": clean_text(data.get("account_number", ""), "เลขบัญชี", 80),
                    "qr_image_url": str(data.get("qr_image_url", ""))[:1000],
                    "updated_at": now_iso(),
                }
                put("payment_settings", settings)
                audit(profile, "UPDATE_PAYMENT_SETTINGS", "settings", "payment_settings", "อัปเดตวิธีชำระเงิน")
                return response(self, 200, {"ok": True, "settings": settings})

            if path.startswith("/api/menus/"):
                require_role(profile, "admin")
                menu_id = path.rsplit("/", 1)[-1]
                current = find_by_id("menus", menu_id)
                if not current:
                    return error_response(self, 404, "ไม่พบเมนู")
                merged = dict(current)
                merged.update(data)
                menu = validate_menu_payload(merged)
                patch(f"menus/{menu_id}", menu)
                audit(profile, "UPDATE_MENU", "menu", menu_id, menu["name"])
                return response(self, 200, {"ok": True, "menu": {**current, **menu, "id": menu_id}})

            if path.startswith("/api/users/"):
                require_role(profile, "admin")
                uid = path.rsplit("/", 1)[-1]
                if "role" in data:
                    update_user_role(uid, data["role"])
                    audit(profile, "UPDATE_ROLE", "user", uid, str(data["role"]))
                if "active" in data:
                    set_user_active(uid, data["active"])
                    audit(profile, "UPDATE_USER_ACTIVE", "user", uid, str(data["active"]))
                return response(self, 200, {"ok": True, "message": "อัปเดตผู้ใช้สำเร็จ"})

            if path.startswith("/api/tables/"):
                require_staff(profile)
                table_id = path.rsplit("/", 1)[-1]
                updates = {}
                if "capacity" in data:
                    require_role(profile, "admin")
                    updates["capacity"] = to_positive_int(data.get("capacity"), "ความจุโต๊ะ")
                if "status" in data:
                    allowed = {"available", "occupied", "waiting_bill", "reserved"}
                    if data.get("status") not in allowed:
                        raise ValueError("สถานะโต๊ะไม่ถูกต้อง")
                    if data.get("status") == "available":
                        table = find_by_id("tables", table_id)
                        orders = get("orders") or {}
                        active = [o for o in (orders.values() if isinstance(orders, dict) else []) if o.get("table_id") == table_id and o.get("status") not in {"closed", "merged", "cancelled"}]
                        if table_members(table):
                            raise ValueError("ยังมี Customer อยู่ในโต๊ะ จึงเปลี่ยนเป็นว่างไม่ได้")
                        for order in active:
                            patch(f"orders/{order.get('id')}", {"status": "cancelled", "cancelled_at": now_iso(), "cancelled_reason": "Staff เปลี่ยนโต๊ะเป็นว่าง"})
                        updates.update({"current_order_id": None, "current_round_id": None, "claimed_by": None, "occupant_ids": [], "party_size": None, "split_requested": False})
                    updates["status"] = data.get("status")
                if not updates:
                    raise ValueError("ไม่มีข้อมูลที่ต้องการอัปเดต")
                patch(f"tables/{table_id}", updates)
                return response(self, 200, {"ok": True, "message": "อัปเดตโต๊ะสำเร็จ"})

            if path.startswith("/api/reservations/"):
                require_staff(profile)
                res_id = path.rsplit("/", 1)[-1]
                reservation = find_by_id("reservations", res_id)
                if not reservation:
                    return error_response(self, 404, "ไม่พบการจอง")
                new_status = data.get("status")
                transitions = {"waiting": {"confirmed", "cancelled"}, "confirmed": {"seated", "cancelled"}}
                if new_status not in transitions.get(reservation.get("status", "waiting"), set()):
                    raise ValueError("ไม่สามารถเปลี่ยนสถานะการจองนี้ได้")
                if new_status == "seated":
                    table = find_table_by_number(reservation.get("table_number"))
                    if not table:
                        raise ValueError("ไม่พบโต๊ะของการจองนี้")
                    if table.get("status") not in ("available", "reserved"):
                        raise ValueError(f"โต๊ะ {table.get('table_number')} ยังไม่ว่าง จึงจัดเข้านั่งไม่ได้")
                    patch(f"tables/{table['id']}", {"status": "occupied"})
                patch(f"reservations/{res_id}", {"status": new_status, "updated_at": now_iso(), "updated_by": profile["id"]})
                audit(profile, "UPDATE_RESERVATION", "reservation", res_id, new_status)
                return response(self, 200, {"ok": True, "message": "อัปเดตการจองสำเร็จ"})

            if path.startswith("/api/table-move-requests/"):
                require_staff(profile)
                request_id = path.rsplit("/", 1)[-1]
                request = find_by_id("table_move_requests", request_id)
                if not request:
                    return error_response(self, 404, "ไม่พบคำขอย้ายโต๊ะ")
                status = data.get("status")
                if status not in {"acknowledged", "completed", "cancelled"}:
                    raise ValueError("สถานะคำขอย้ายโต๊ะไม่ถูกต้อง")
                if status == "completed":
                    old_table = find_by_id("tables", request.get("table_id"))
                    new_table = find_by_id("tables", request.get("new_table_id"))
                    if not old_table or not new_table or new_table.get("status") != "available":
                        raise ValueError("โต๊ะปลายทางไม่ว่างหรือไม่พบโต๊ะ")
                    order_id = old_table.get("current_order_id")
                    members = table_members(old_table)
                    presence = old_table.get("member_last_seen") if isinstance(old_table.get("member_last_seen"), dict) else {}
                    old_order = find_by_id("orders", order_id) if order_id else None
                    patch(f"tables/{request['table_id']}", {"status": "available", "current_order_id": None, "current_round_id": None, "claimed_by": None, "occupant_ids": [], "member_last_seen": {}, "party_size": None})
                    patch(f"tables/{request['new_table_id']}", {"status": "occupied", "current_order_id": order_id, "current_round_id": old_table.get("current_round_id") or (old_order or {}).get("round_id") or order_id, "claimed_by": old_table.get("claimed_by") or (members[0] if members else request.get("customer_id")), "occupant_ids": members or [request.get("customer_id")], "member_last_seen": presence, "party_size": old_table.get("party_size")})
                    if order_id:
                        patch(f"orders/{order_id}", {"table_id": request["new_table_id"], "table_number": new_table.get("table_number"), "moved_from_table": old_table.get("table_number"), "moved_to_table": new_table.get("table_number"), "moved_at": now_iso(), "move_label": f"ย้ายจากโต๊ะ {old_table.get('table_number')} → โต๊ะ {new_table.get('table_number')}"})
                        kitchen = get("kitchen") or {}
                        for kid, item in (kitchen.items() if isinstance(kitchen, dict) else []):
                            if item.get("order_id") == order_id:
                                patch(f"kitchen/{kid}", {"table_number": new_table.get("table_number"), "moved_from_table": old_table.get("table_number"), "moved_to_table": new_table.get("table_number"), "moved_at": now_iso(), "move_label": f"ย้ายจากโต๊ะ {old_table.get('table_number')} → โต๊ะ {new_table.get('table_number')}"})
                    move_active_orders(request["table_id"], request["new_table_id"], old_table.get("table_number"), new_table.get("table_number"))
                    patch(f"table_move_requests/{request_id}", {"status": status, "updated_at": now_iso(), "updated_by": profile.get("id")})
                    return response(self, 200, {"ok": True, "message": "ย้ายโต๊ะจริงสำเร็จ"})
                patch(f"table_move_requests/{request_id}", {"status": status, "updated_at": now_iso(), "updated_by": profile.get("id")})
                return response(self, 200, {"ok": True, "message": "อัปเดตคำขอย้ายโต๊ะแล้ว"})
            if path.startswith("/api/kitchen/"):
                require_staff(profile)
                item_id = path.rsplit("/", 1)[-1]
                status = data.get("status")
                if status not in {"pending", "cooking", "done"}:
                    raise ValueError("สถานะครัวไม่ถูกต้อง")
                patch(f"kitchen/{item_id}", {"status": status, "updated_at": now_iso()})
                return response(self, 200, {"ok": True, "message": "อัปเดตสถานะครัวสำเร็จ"})

            if path.startswith("/api/orders/") and path.endswith("/request-checkout"):
                require_role(profile, "customer")
                order_id = path.split("/")[-2]
                order = find_by_id("orders", order_id)
                bill_table = find_by_id("tables", order.get("table_id")) if order else None
                # The current bill is shared by the table. Any active table member may request it,
                # even when the central bill has no customer_id or belongs to the first member.
                if not order or not (order.get("customer_id") == profile.get("id") or (bill_table and table_has_member(bill_table, profile.get("id")))):
                    return error_response(self, 404, "ไม่พบบิลของคุณ")
                if order.get("status") in ("closed", "merged", "cancelled"):
                    raise ValueError("บิลนี้ถูกยกเลิกหรือปิดแล้ว ไม่สามารถเช็คบิลซ้ำได้")
                if order.get("status") == "waiting_bill":
                    return response(self, 200, {"ok": True, "already_requested": True, "message": "เรียกเช็คบิลแล้ว กำลังรอพนักงาน"})
                requested_points = int(data.get("points_to_use", 0) or 0)
                if requested_points < 0 or requested_points % 100 != 0:
                    raise ValueError("การใช้แต้มต้องใช้ครั้งละ 100 แต้ม")
                customer = get_profile(profile["id"]) or profile
                available_points = int(customer.get("member_points", 0) or 0)
                if requested_points > available_points:
                    raise ValueError("แต้มไม่เพียงพอ")
                points_discount(order.get("items", []), requested_points)
                patch(f"orders/{order_id}", {"status": "waiting_bill", "checkout_requested_at": now_iso(), "points_to_use": requested_points, "points_customer_id": profile.get("id")})
                table_id = order.get("table_id")
                if table_id:
                    patch(f"tables/{table_id}", {"status": "waiting_bill"})
                notify_staff("checkout_request", "ลูกค้าเรียกเช็คบิล", f"โต๊ะ {bill_table.get('table_number') if bill_table else '-'}", order_id)
                return response(self, 200, {"ok": True, "message": "เรียกพนักงานเช็คบิลแล้ว"})
            if path.startswith("/api/orders/") and path.endswith("/checkout"):
                require_staff(profile)
                order_id = path.split("/")[-2]
                order = find_by_id("orders", order_id)
                if not order:
                    return error_response(self, 404, "ไม่พบออเดอร์")
                if order.get("status") in ("closed", "merged"):
                    return error_response(self, 400, "ไม่สามารถเช็คบิลซ้ำได้")
                if order.get("status") == "cancelled":
                    return error_response(self, 400, "บิลนี้ถูกยกเลิกแล้ว ไม่สามารถเช็คบิลได้")
                if order.get("status") != "waiting_bill":
                    return error_response(self, 400, "ต้องรอ Customer เรียกเช็คบิลก่อน")
                if order.get("checkout_lock"):
                    return error_response(self, 409, "บิลนี้กำลังถูกเช็คบิลโดยพนักงานคนอื่น")
                patch(f"orders/{order_id}", {"checkout_lock": {"staff_id": profile.get("id"), "at": now_iso()}})
                order["checkout_lock"] = True
                payment_method = str(data.get("payment_method") or "cash").strip().lower()
                payment_key = {"cash": "cash_enabled", "bank": "bank_enabled", "qr": "qr_enabled"}.get(payment_method)
                payment_settings = {**DEFAULT_PAYMENT_SETTINGS, **(get("payment_settings") or {})}
                if not payment_key or not payment_settings.get(payment_key):
                    patch(f"orders/{order_id}", {"checkout_lock": None})
                    raise ValueError("วิธีชำระเงินนี้ยังไม่ได้เปิดใช้งานโดย Admin")
                discount = to_positive_number(data.get("discount", 0), "ส่วนลด", allow_zero=True)
                requested_points, points_reduction, subtotal = points_discount(order.get("items", []), order.get("points_to_use", 0))
                if discount + points_reduction > subtotal:
                    raise ValueError("ส่วนลดรวมเกินยอดอาหาร")
                bill = calculate_bill(order.get("items", []), discount + points_reduction)
                bill["manual_discount"] = round(discount, 2)
                bill["points_used"] = requested_points
                bill["points_discount"] = points_reduction
                # Earn points per item owner, not only from the first customer
                # who opened the shared table bill.
                earned_by_customer = {}
                has_item_owner = any(item.get("customer_id") for item in order.get("items", []))
                for item in order.get("items", []):
                    # After split-by-customer, use the current owner on each
                    # item. Unassigned central/counter items do not belong to
                    # a customer and must not be credited to the first guest.
                    owner_id = item.get("customer_id")
                    if not owner_id and not has_item_owner:
                        owner_id = order.get("customer_id")
                    if not owner_id:
                        continue
                    item_total = float(item.get("unit_price", 0) or 0) * int(item.get("quantity", 0) or 0)
                    earned_by_customer[str(owner_id)] = earned_by_customer.get(str(owner_id), 0) + item_total
                earned_by_customer = {uid: int(amount // 10) for uid, amount in earned_by_customer.items()}
                earned_points = sum(earned_by_customer.values())
                points_owner = str(order.get("points_customer_id") or order.get("customer_id") or "")
                points_owner_points = 0
                if points_owner:
                    points_owner_points = int((get_profile(points_owner) or {}).get("member_points", 0) or 0)
                    if requested_points > points_owner_points:
                        raise ValueError("แต้มของลูกค้าไม่เพียงพอ")
                closed = {**bill, "earned_points": earned_points, "status": "closed", "checkout_lock": None, "closed_at": now_iso(), "closed_by": profile["id"], "payment_method": payment_method}
                patch(f"orders/{order_id}", closed)
                table_id = order.get("table_id")
                if table_id:
                    all_orders = get("orders") or {}
                    remaining = [o for o in (all_orders.values() if isinstance(all_orders, dict) else []) if o.get("table_id") == table_id and o.get("id") != order_id and o.get("status") not in ("closed", "merged", "cancelled")]
                    if remaining:
                        next_order = remaining[0]
                        next_status = "waiting_bill" if next_order.get("status") == "waiting_bill" else "occupied"
                        patch(f"tables/{table_id}", {"status": next_status, "current_order_id": next_order.get("id"), "current_round_id": next_order.get("round_id") or next_order.get("id")})
                    else:
                        patch(f"tables/{table_id}", {"status": "available", "current_order_id": None, "current_round_id": None, "claimed_by": None, "occupant_ids": [], "party_size": None, "split_requested": False})
                member_points_after = {}
                for uid, earned_for_user in earned_by_customer.items():
                    current = int((get_profile(uid) or {}).get("member_points", 0) or 0)
                    deduction = requested_points if uid == points_owner else 0
                    new_points = current - deduction + earned_for_user
                    patch(f"users/{uid}", {"member_points": new_points})
                    member_points_after[uid] = new_points
                if points_owner and points_owner not in member_points_after:
                    new_points = points_owner_points - requested_points
                    patch(f"users/{points_owner}", {"member_points": new_points})
                    member_points_after[points_owner] = new_points
                closed["member_points_after"] = member_points_after
                closed["earned_points_by_customer"] = earned_by_customer
                audit(profile, "CHECKOUT_ORDER", "order", order_id, str(bill["total"]))
                notify_staff("checkout_done", "ปิดบิลสำเร็จ", f"โต๊ะ {order.get('table_number', '-')}", order_id)
                return response(self, 200, {"ok": True, "order": {**order, **closed}})

            return error_response(self, 404, "ไม่พบ API ที่ร้องขอ")
        except AuthError as exc:
            return error_response(self, 403, str(exc))
        except (ValueError, FirebaseError) as exc:
            return error_response(self, 400, str(exc))
        except Exception:
            return error_response(self, 500, "เกิดข้อผิดพลาดภายในระบบ กรุณาลองใหม่")

    def do_DELETE(self):
        try:
            path = urlparse(self.path).path
            profile, _ = require_user(self.headers)
            if path.startswith("/api/menus/"):
                require_role(profile, "admin")
                menu_id = path.rsplit("/", 1)[-1]
                if not find_by_id("menus", menu_id):
                    return error_response(self, 404, "ไม่พบเมนู")
                delete(f"menus/{menu_id}")
                audit(profile, "DELETE_MENU", "menu", menu_id)
                return response(self, 200, {"ok": True, "message": "ลบเมนูสำเร็จ"})
            return error_response(self, 404, "ไม่พบ API ที่ร้องขอ")
        except AuthError as exc:
            return error_response(self, 403, str(exc))
        except Exception:
            return error_response(self, 500, "ไม่สามารถดำเนินการได้")


def ensure_local_admin():
    """Seed a usable Admin account once in Local mode for first-time setup."""
    if not is_local_mode():
        return
    try:
        users = get("users") or {}
        if isinstance(users, dict) and any(isinstance(u, dict) and u.get("role") == "admin" for u in users.values()):
            return
        admin_email = os.environ.get("LOCAL_ADMIN_EMAIL", "admin@itailaew.com")
        admin_password = os.environ.get("LOCAL_ADMIN_PASSWORD", "Admin@12345")
        auth = firebase_signup(admin_email, admin_password)
        create_profile(auth["localId"], admin_email, "itailaew Admin", "admin", password=admin_password)
        print(f"Local Admin created: {admin_email} / {admin_password}")
    except Exception as exc:
        print(f"Local Admin seed skipped: {exc}")



if __name__ == "__main__":
    ensure_local_admin()
    # Local development only. Vercel imports `handler` and does not execute this block.
    host = "127.0.0.1"
    port = 8000
    print(f"itailaew API running at http://{host}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        HTTPServer((host, port), handler).serve_forever()
    except KeyboardInterrupt:
        print("\nitailaew API stopped.")
    except Exception as exc:
        print(f"ไม่สามารถเริ่ม Local API ได้: {exc}")
