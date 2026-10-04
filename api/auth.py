import base64
import hashlib
import hmac
import json
import re
import time
import urllib.parse
import urllib.request
import urllib.error

try:
    from .config import FIREBASE_API_KEY, ALLOWED_ROLES, LOCAL_AUTH_SECRET, is_local_mode
    from .firebase import get, put, patch, post, FirebaseError
except ImportError:
    from config import FIREBASE_API_KEY, ALLOWED_ROLES, LOCAL_AUTH_SECRET, is_local_mode
    from firebase import get, put, patch, post, FirebaseError

ALLOWED_EMAIL_DOMAINS = {"gmail.com", "outlook.com", "hotmail.com", "kkumail.com"}
EMAIL_RE = re.compile(r"^[^@\s]+@([^@\s]+)$")


class AuthError(Exception):
    pass


def _auth_request(action, payload):
    try:
        if is_local_mode():
            raise AuthError("Local mode")
        url = f"https://identitytoolkit.googleapis.com/v1/accounts:{action}?key={urllib.parse.quote(FIREBASE_API_KEY)}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=12) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            message = detail.get("error", {}).get("message", "Authentication failed")
        except Exception:
            message = "Authentication failed"
        raise AuthError(_friendly_auth_error(message))
    except AuthError:
        raise
    except Exception as exc:
        raise AuthError(f"ระบบยืนยันตัวตนขัดข้อง: {exc}")


def _friendly_auth_error(code):
    messages = {
        "EMAIL_EXISTS": "อีเมลนี้ถูกใช้งานแล้ว กรุณาใช้อีเมลอื่น",
        "INVALID_PASSWORD": "อีเมลหรือรหัสผ่านไม่ถูกต้อง",
        "EMAIL_NOT_FOUND": "อีเมลหรือรหัสผ่านไม่ถูกต้อง",
        "INVALID_LOGIN_CREDENTIALS": "อีเมลหรือรหัสผ่านไม่ถูกต้อง",
        "WEAK_PASSWORD : Password should be at least 6 characters": "รหัสผ่านต้องมีอย่างน้อย 6 ตัวอักษร",
        "OPERATION_NOT_ALLOWED": "ยังไม่ได้เปิดใช้งาน Email/Password ใน Firebase Authentication",
        "TOO_MANY_ATTEMPTS_TRY_LATER": "มีการลองเข้าสู่ระบบบ่อยเกินไป กรุณาลองใหม่ภายหลัง",
    }
    return messages.get(code, code.replace("_", " ").strip().title() if isinstance(code, str) else "Authentication failed")


def _hash_password(password, salt=None):
    try:
        salt = salt or hashlib.sha256(f"{time.time_ns()}".encode("utf-8")).hexdigest()[:32]
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
        return f"pbkdf2_sha256$120000${salt}${digest.hex()}"
    except Exception as exc:
        raise AuthError(f"ไม่สามารถสร้างรหัสผ่านได้: {exc}")


def _check_password(password, encoded):
    try:
        scheme, rounds, salt, expected = str(encoded).split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), int(rounds)).hex()
        return hmac.compare_digest(digest, expected)
    except Exception:
        return False


def _local_token(uid):
    issued = str(int(time.time()))
    raw = f"{uid}.{issued}"
    signature = hmac.new(LOCAL_AUTH_SECRET.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest()
    payload = f"{raw}.{signature}".encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _verify_local_token(token):
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        uid, issued, signature = raw.rsplit(".", 2)
        message = f"{uid}.{issued}"
        expected = hmac.new(LOCAL_AUTH_SECRET.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise AuthError("Token ไม่ถูกต้อง")
        if time.time() - int(issued) > 60 * 60 * 24:
            raise AuthError("Session หมดอายุ กรุณา Login ใหม่")
        return uid
    except AuthError:
        raise
    except Exception:
        raise AuthError("Token ไม่ถูกต้องหรือหมดอายุ")


def firebase_signup(email, password):
    if is_local_mode():
        existing = get("users") or {}
        for user in existing.values() if isinstance(existing, dict) else []:
            if isinstance(user, dict) and user.get("email", "").lower() == email.lower():
                raise AuthError("อีเมลนี้ถูกใช้งานแล้ว กรุณาใช้อีเมลอื่น")
        import uuid
        uid = f"local_{uuid.uuid4().hex[:16]}"
        return {"localId": uid, "idToken": _local_token(uid), "email": email}
    return _auth_request("signUp", {"email": email, "password": password, "returnSecureToken": True})


def firebase_signin(email, password):
    if is_local_mode():
        users = get("users") or {}
        if not isinstance(users, dict):
            raise AuthError("ไม่พบข้อมูลผู้ใช้งาน")
        for uid, user in users.items():
            if isinstance(user, dict) and user.get("email", "").lower() == email.lower():
                if not _check_password(password, user.get("password_hash", "")):
                    raise AuthError("อีเมลหรือรหัสผ่านไม่ถูกต้อง")
                if user.get("active", True) is False:
                    raise AuthError("บัญชีนี้ถูกระงับการใช้งาน")
                return {"localId": uid, "idToken": _local_token(uid), "email": email}
        raise AuthError("อีเมลหรือรหัสผ่านไม่ถูกต้อง")
    return _auth_request("signInWithPassword", {"email": email, "password": password, "returnSecureToken": True})


def firebase_refresh(refresh_token):
    if not refresh_token:
        raise AuthError("ไม่พบ Refresh Token")
    if is_local_mode():
        uid = _verify_local_token(refresh_token)
        return {"localId": uid, "id_token": _local_token(uid), "refresh_token": refresh_token}
    try:
        url = f"https://securetoken.googleapis.com/v1/token?key={urllib.parse.quote(FIREBASE_API_KEY)}"
        body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh_token}).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        with urllib.request.urlopen(req, timeout=12) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError:
        raise AuthError("Refresh Token ไม่ถูกต้อง กรุณา Login ใหม่")


def verify_id_token(id_token):
    if not id_token:
        raise AuthError("ไม่พบ Token")
    if is_local_mode():
        uid = _verify_local_token(id_token)
        return {"localId": uid}
    data = _auth_request("lookup", {"idToken": id_token})
    users = data.get("users", [])
    if not users:
        raise AuthError("Token ไม่ถูกต้องหรือหมดอายุ")
    return users[0]


def _public_profile(profile):
    safe = dict(profile)
    safe.pop("password_hash", None)
    return safe


def get_profile(uid):
    profile = get(f"users/{uid}")
    if not profile:
        raise AuthError("ไม่พบข้อมูลผู้ใช้งาน")
    role = profile.get("role", "customer")
    if role not in ALLOWED_ROLES:
        raise AuthError("Role ไม่ถูกต้อง")
    if profile.get("active", True) is False:
        raise AuthError("บัญชีนี้ถูกระงับการใช้งาน")
    return _public_profile(profile)


def require_user(headers):
    try:
        auth_header = headers.get("Authorization", "")
        token = auth_header.split(" ", 1)[1] if auth_header.startswith("Bearer ") else ""
        identity = verify_id_token(token)
        uid = identity.get("localId")
        profile = get_profile(uid)
        return profile, token
    except Exception as exc:
        if isinstance(exc, AuthError):
            raise
        raise AuthError(f"ตรวจสอบผู้ใช้งานไม่สำเร็จ: {exc}")


def require_role(profile, *roles):
    if profile.get("role") not in roles:
        raise AuthError("ไม่มีสิทธิ์เข้าถึง")


def safe_email(email):
    if not isinstance(email, str):
        return False
    match = EMAIL_RE.match(email.strip().lower())
    return bool(match and match.group(1) in ALLOWED_EMAIL_DOMAINS)


def validate_password(password):
    return isinstance(password, str) and len(password) >= 6


def create_profile(uid, email, name, role="customer", password=None):
    if not re.fullmatch(r"[A-Za-z\u0E00-\u0E7F]+(?: +[A-Za-z\u0E00-\u0E7F]+)*", str(name or "").strip()):
        raise AuthError("ชื่อใช้ได้เฉพาะตัวอักษรไทย/อังกฤษและเว้นวรรค")
    if role not in ALLOWED_ROLES:
        raise AuthError("Role ไม่ถูกต้อง")
    profile = {
        "id": uid, "email": email.strip().lower(), "name": name.strip(),
        "role": role, "member_points": 0, "active": True
    }
    if is_local_mode():
        if password is None:
            raise AuthError("ไม่พบรหัสผ่านสำหรับ Local account")
        profile["password_hash"] = _hash_password(password)
    put(f"users/{uid}", profile)
    return _public_profile(profile)


def list_users():
    data = get("users") or {}
    return [_public_profile(user) for user in data.values()] if isinstance(data, dict) else []


def update_user_role(uid, role):
    if role not in ALLOWED_ROLES:
        raise AuthError("Role ไม่ถูกต้อง")
    return patch(f"users/{uid}", {"role": role})


def set_user_active(uid, active):
    return patch(f"users/{uid}", {"active": bool(active)})
