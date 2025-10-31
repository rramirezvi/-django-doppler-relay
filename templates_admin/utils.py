import os


BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ATTACH_DIR = os.path.join(BASE_DIR, "attachments", "templates")


def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def cache_path_for(template_id: str) -> str:
    _ensure_dir(ATTACH_DIR)
    safe_id = str(template_id).strip()
    return os.path.join(ATTACH_DIR, f"{safe_id}.html")


def read_cached_html(template_id: str) -> str:
    try:
        p = cache_path_for(template_id)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass
    return ""


def write_cached_html(template_id: str, html: str) -> None:
    try:
        p = cache_path_for(template_id)
        with open(p, "w", encoding="utf-8") as f:
            f.write(html or "")
    except Exception:
        # No romper el flujo si falla el caché
        pass

