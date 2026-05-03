import io
import json
import os
import time
import warnings
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Any

import requests
from PIL import Image, ImageDraw
import re

# =========================
# WARNINGS
# =========================
warnings.filterwarnings("ignore", category=Image.DecompressionBombWarning)

# =========================
# CONFIG
# =========================
GOES19_URL = os.environ.get(
    "GOES19_URL",
    "https://cdn.star.nesdis.noaa.gov/GOES19/ABI/FD/GEOCOLOR/latest.jpg",
)

MAX_SIZE = int(os.environ.get("MAX_SIZE", "1024"))

ROBLOX_API_KEY = os.environ.get("ROBLOX_API_KEY")
ROBLOX_USER_ID = int(os.environ.get("ROBLOX_USER_ID", "3538598020"))
ROBLOX_GROUP_ID = int(os.environ.get("ROBLOX_GROUP_ID", "0"))

OUT_FILE = os.environ.get("OUT_FILE", "latest_decal_id.txt")

DEFAULT_TIMEOUT = 120
DOWNLOAD_TIMEOUT = 60
MAX_RETRIES = 3

ROBLOX_UPLOAD_URL = "https://apis.roblox.com/assets/v1/assets"
ROBLOX_OPERATIONS_URL = "https://apis.roblox.com/assets/v1/operations"

# =========================
# DATA
# =========================
@dataclass(frozen=True)
class RobloxConfig:
    api_key: str
    user_id: int
    group_id: int

# =========================
# HTTP
# =========================

def _headers(cfg: RobloxConfig) -> dict:
    return {"x-api-key": cfg.api_key}


def _http_request(
    method: str,
    url: str,
    *,
    headers: dict,
    files: Optional[dict] = None,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = MAX_RETRIES,
) -> requests.Response:
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            r = requests.request(
                method,
                url,
                headers=headers,
                files=files,
                timeout=timeout,
            )

            if r.status_code >= 400:
                raise requests.HTTPError(f"{r.status_code}: {r.text}")

            return r

        except Exception as exc:
            last_error = exc
            print(f"⚠️ erro HTTP (tentativa {attempt}/{retries}): {exc}")
            time.sleep(2 * attempt)

    raise RuntimeError("Falha HTTP após retries") from last_error

# =========================
# UTIL
# =========================

def extrair_asset_id(obj: Any) -> Optional[str]:
    if isinstance(obj, dict):
        for k in ("assetId", "asset_id", "assetID"):
            v = obj.get(k)
            if isinstance(v, (int, str)):
                v = str(v)
                if v.isdigit():
                    return v

        for k in ("path", "response", "result", "metadata"):
            sub = obj.get(k)
            if isinstance(sub, dict) and isinstance(sub.get("path"), str):
                m = re.search(r"assets/(\\d+)", sub["path"])
                if m:
                    return m.group(1)

    return None


def validar_asset_id(cfg: RobloxConfig, asset_id: str) -> bool:
    url = f"{ROBLOX_UPLOAD_URL}/{asset_id}"
    r = _http_request("GET", url, headers=_headers(cfg))
    data = r.json()
    return (data.get("path") or "").startswith(f"assets/{asset_id}")

# =========================
# GOES
# =========================

def baixar_goes19() -> bytes:
    print("📡 baixando GOES-19")
    r = _http_request("GET", GOES19_URL, headers={}, timeout=DOWNLOAD_TIMEOUT)
    return r.content

# =========================
# IMAGE
# =========================

def recorte_circular_png(image_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")

    w, h = img.size
    size = min(w, h)

    left = (w - size) // 2
    top = (h - size) // 2

    img = img.crop((left, top, left + size, top + size))

    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size - 1, size - 1), fill=255)

    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)

    if size > MAX_SIZE:
        out = out.resize((MAX_SIZE, MAX_SIZE), Image.LANCZOS)

    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()

# =========================
# ROBLOX
# =========================

def esperar_operation(cfg: RobloxConfig, op_id: str) -> str:
    url = f"{ROBLOX_OPERATIONS_URL}/{op_id}"
    start = time.time()

    # 10 minutos de timeout (evita crash em operações lentas)
    TIMEOUT = 600

    while True:
        if time.time() - start > TIMEOUT:
            print("⚠️ operação demorou demais, mas o script não vai quebrar imediatamente")
            raise RuntimeError("timeout operação (10 min)")

        r = _http_request("GET", url, headers=_headers(cfg))
        data = r.json()

        asset_id = extrair_asset_id(data)
        if asset_id and validar_asset_id(cfg, asset_id):
            return asset_id

        if str(data.get("status", "")).lower() in ("done", "completed", "success"):
            raise RuntimeError("terminou sem asset válido")

        time.sleep(3)


def upload_decal_grupo(cfg: RobloxConfig, png: bytes) -> str:
    if cfg.group_id <= 0:
        raise RuntimeError("groupId inválido")

    payload = {
        "assetType": "Decal",
        "displayName": f"GOES19_{datetime.utcnow().isoformat()}",
        "description": "GOES19 auto",
        "creationContext": {"creator": {"groupId": cfg.group_id}},
    }

    files = {
        "request": (None, json.dumps(payload), "application/json"),
        "fileContent": ("img.png", png, "image/png"),
    }

    r = _http_request(
        "POST",
        ROBLOX_UPLOAD_URL,
        headers=_headers(cfg),
        files=files,
    )

    data = r.json()
    op_id = data.get("operationId")

    if not op_id:
        raise RuntimeError("sem operationId")

    return esperar_operation(cfg, op_id)

# =========================
# MAIN
# =========================

def build_config() -> RobloxConfig:
    if not ROBLOX_API_KEY:
        raise RuntimeError("sem api key")

    return RobloxConfig(
        api_key=ROBLOX_API_KEY,
        user_id=ROBLOX_USER_ID,
        group_id=ROBLOX_GROUP_ID,
    )


def main():
    cfg = build_config()

    raw = baixar_goes19()
    png = recorte_circular_png(raw)

    asset_id = upload_decal_grupo(cfg, png)

    with open(OUT_FILE, "w") as f:
        f.write(asset_id)

    print("OK:", asset_id)


if __name__ == "__main__":
    main()
