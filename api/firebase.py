import copy
import json
import os
import threading
import time
import urllib.parse
import urllib.request
import urllib.error

try:
    from .config import FIREBASE_DB_URL, FIREBASE_DB_SECRET, LOCAL_DB_PATH, is_local_mode
except ImportError:
    from config import FIREBASE_DB_URL, FIREBASE_DB_SECRET, LOCAL_DB_PATH, is_local_mode


class FirebaseError(Exception):
    pass


_FILE_LOCK = threading.RLock()
_GET_CACHE = {}
_GET_CACHE_TTL = 0.25


def _url(path, auth=True):
    clean = "/".join(str(path).strip("/").split("/"))
    url = f"{FIREBASE_DB_URL}/{clean}.json"
    if auth and FIREBASE_DB_SECRET:
        url += "?" + urllib.parse.urlencode({"auth": FIREBASE_DB_SECRET})
    return url


def _ensure_local_file():
    try:
        folder = os.path.dirname(LOCAL_DB_PATH)
        if folder:
            os.makedirs(folder, exist_ok=True)
        if not os.path.exists(LOCAL_DB_PATH):
            with open(LOCAL_DB_PATH, "w", encoding="utf-8") as handle:
                json.dump({"users": {}, "menus": {}, "tables": {}, "reservations": {}, "orders": {}, "kitchen": {}, "audit_logs": {}}, handle, ensure_ascii=False, indent=2)
    except Exception as exc:
        raise FirebaseError(f"ไม่สามารถเตรียมไฟล์ฐานข้อมูล Local ได้: {exc}")


def _read_local():
    _ensure_local_file()
    try:
        with _FILE_LOCK, open(LOCAL_DB_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("ฐานข้อมูล Local ต้องเป็น JSON object")
        return data
    except Exception as exc:
        raise FirebaseError(f"อ่านฐานข้อมูล Local ไม่สำเร็จ: {exc}")


def _write_local(data):
    try:
        _ensure_local_file()
        temp_path = f"{LOCAL_DB_PATH}.tmp"
        with _FILE_LOCK:
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            os.replace(temp_path, LOCAL_DB_PATH)
    except Exception as exc:
        raise FirebaseError(f"บันทึกฐานข้อมูล Local ไม่สำเร็จ: {exc}")


def _split_path(path):
    return [part for part in str(path).strip("/").split("/") if part]


def _local_get(path):
    data = _read_local()
    parts = _split_path(path)
    node = data
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return copy.deepcopy(node)


def _local_set(path, payload):
    data = _read_local()
    parts = _split_path(path)
    if not parts:
        if not isinstance(payload, dict):
            raise FirebaseError("ฐานข้อมูลรากต้องเป็น object")
        data = payload
    else:
        node = data
        for part in parts[:-1]:
            existing = node.get(part)
            if not isinstance(existing, dict):
                existing = {}
                node[part] = existing
            node = existing
        node[parts[-1]] = copy.deepcopy(payload)
    _write_local(data)
    return copy.deepcopy(payload)


def _deep_merge(target, patch_data):
    if not isinstance(target, dict) or not isinstance(patch_data, dict):
        return copy.deepcopy(patch_data)
    result = copy.deepcopy(target)
    for key, value in patch_data.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _local_patch(path, payload):
    current = _local_get(path)
    if current is None:
        current = {}
    merged = _deep_merge(current, payload)
    return _local_set(path, merged)


def _local_post(path, payload):
    import uuid
    key = f"{uuid.uuid4().hex[:20]}"
    _local_set(f"{str(path).strip('/')}/{key}", payload)
    return {"name": key}


def _local_delete(path):
    data = _read_local()
    parts = _split_path(path)
    if not parts:
        return _write_local({})
    node = data
    for part in parts[:-1]:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    node.pop(parts[-1], None)
    _write_local(data)
    return None


def request(method, path, payload=None, auth=True, timeout=12):
    try:
        cache_key = (method, str(path), bool(auth))
        if method == "GET":
            cached = _GET_CACHE.get(cache_key)
            if cached and (time.time() - cached[0]) < _GET_CACHE_TTL:
                return copy.deepcopy(cached[1])
        if method != "GET":
            _GET_CACHE.clear()
        if is_local_mode():
            if method == "GET":
                result = _local_get(path)
                _GET_CACHE[cache_key] = (time.time(), copy.deepcopy(result))
                return result
            if method == "PUT":
                return _local_set(path, payload)
            if method == "PATCH":
                return _local_patch(path, payload)
            if method == "POST":
                return _local_post(path, payload)
            if method == "DELETE":
                return _local_delete(path)
            raise FirebaseError(f"ไม่รองรับ Local HTTP method: {method}")

        if not FIREBASE_DB_URL:
            raise FirebaseError("ยังไม่ได้ตั้งค่า FIREBASE_DB_URL")
        body = None
        headers = {"Content-Type": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(_url(path, auth=auth), data=body, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            result = json.loads(raw) if raw else None
            if method == "GET":
                _GET_CACHE[cache_key] = (time.time(), copy.deepcopy(result))
            return result
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
        except Exception:
            detail = ""
        raise FirebaseError(f"Firebase HTTP {exc.code}: {detail[:300]}")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise FirebaseError(f"เชื่อมต่อฐานข้อมูลไม่สำเร็จ: {exc}")
    except (ValueError, TypeError) as exc:
        raise FirebaseError(f"ข้อมูลจากฐานข้อมูลไม่ถูกต้อง: {exc}")
    except FirebaseError:
        raise
    except Exception as exc:
        raise FirebaseError(f"Database error: {exc}")


def get(path):
    return request("GET", path)


def put(path, payload):
    return request("PUT", path, payload)


def patch(path, payload):
    return request("PATCH", path, payload)


def post(path, payload):
    return request("POST", path, payload)


def delete(path):
    return request("DELETE", path)


def transaction(path, updater, retries=5):
    """Atomically update one Firebase node using ETags."""
    if is_local_mode():
        with _FILE_LOCK:
            current = _local_get(path)
            updated = updater(copy.deepcopy(current))
            return _local_set(path, updated)
    if not FIREBASE_DB_URL:
        raise FirebaseError("ยังไม่ได้ตั้งค่า FIREBASE_DB_URL")
    for _ in range(max(1, retries)):
        url = _url(path)
        get_req = urllib.request.Request(url, headers={"X-Firebase-ETag": "true"}, method="GET")
        try:
            with urllib.request.urlopen(get_req, timeout=12) as res:
                raw = res.read().decode("utf-8")
                current = json.loads(raw) if raw else None
                etag = res.headers.get("ETag")
            updated = updater(copy.deepcopy(current))
            body = json.dumps(updated, ensure_ascii=False).encode("utf-8")
            put_req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "If-Match": etag or "*"}, method="PUT")
            with urllib.request.urlopen(put_req, timeout=12) as res:
                raw = res.read().decode("utf-8")
                _GET_CACHE.clear()
                return json.loads(raw) if raw else updated
        except urllib.error.HTTPError as exc:
            if exc.code == 412:
                continue
            try:
                detail = exc.read().decode("utf-8")[:300]
            except Exception:
                detail = str(exc)
            raise FirebaseError(f"Firebase transaction HTTP {exc.code}: {detail}")
        except (urllib.error.URLError, TimeoutError) as exc:
            raise FirebaseError(f"เชื่อมต่อฐานข้อมูลไม่สำเร็จ: {exc}")
    raise FirebaseError("ข้อมูลโต๊ะมีการเปลี่ยนพร้อมกันหลายคน กรุณากดเลือกโต๊ะอีกครั้ง")
