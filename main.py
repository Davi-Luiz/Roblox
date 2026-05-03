import io
import json
import os
import re
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import requests
from PIL import Image, ImageDraw

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

DEFAULT_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT_SECONDS", "120"))
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT_SECONDS", "60"))
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))

OPERATION_TIMEOUT_SECONDS = int(os.environ.get("OPERATION_TIMEOUT_SECONDS", "1800"))
POLL_INTERVAL_SECONDS = float(os.environ.get("POLL_INTERVAL_SECONDS", "3"))

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
    last_error: Optional[Exception] = None

    for attempt in range(1, retries + 1):
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                files=files,
                timeout=timeout,
            )

            if response.status_code >= 400:
                raise requests.HTTPError(f"{response.status_code}: {response.text}")

            return response

        except Exception as exc:
            last_error = exc
            print(f"⚠️ erro HTTP (tentativa {attempt}/{retries}): {exc}")
            if attempt < retries:
                time.sleep(2 * attempt)

    raise RuntimeError("Falha HTTP após retries") from last_error


# =========================
# UTIL
# =========================
def extrair_asset_id(obj: Any) -> Optional[str]:
    if isinstance(obj, dict):
        for key in ("assetId", "asset_id", "assetID"):
            value = obj.get(key)
            if isinstance(value, (int, str)):
                value = str(value)
                if value.isdigit():
                    return value

        for key in ("path", "response", "result", "metadata"):
            sub = obj.get(key)
            if isinstance(sub, dict):
                path = sub.get("path")
                if isinstance(path, str):
                    match = re.search(r"assets/(\d+)", path)
                    if match:
                        return match.group(1)

        path = obj.get("path")
        if isinstance(path, str):
            match = re.search(r"assets/(\d+)", path)
            if match:
                return match.group(1)

    return None


def validar_asset_id(cfg: RobloxConfig, asset_id: str) -> bool:
    try:
        url = f"{ROBLOX_UPLOAD_URL}/{asset_id}"
        response = _http_request("GET", url, headers=_headers(cfg))
        data = response.json()
        return isinstance(data, dict) and str(data.get("path", "")).startswith(f"assets/{asset_id}")
    except Exception as exc:
        print(f"⚠️ não foi possível validar asset_id {asset_id}: {exc}")
        return False


# =========================
# GOES
# =========================
def baixar_goes19() -> bytes:
    print("📡 baixando GOES-19")
    response = _http_request("GET", GOES19_URL, headers={}, timeout=DOWNLOAD_TIMEOUT)
    return response.content


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
def esperar_operation(cfg: RobloxConfig, op_id: str) -> Optional[str]:
    url = f"{ROBLOX_OPERATIONS_URL}/{op_id}"
    start = time.monotonic()

    while True:
        elapsed = time.monotonic() - start
        if elapsed > OPERATION_TIMEOUT_SECONDS:
            print(
                f"⚠️ operação demorou mais que {OPERATION_TIMEOUT_SECONDS}s. "
                "Vou encerrar sem quebrar o processo."
            )
            return None

        try:
            response = _http_request("GET", url, headers=_headers(cfg))
            data = response.json()
        except Exception as exc:
            print(f"⚠️ falha ao consultar operação {op_id}: {exc}")
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        asset_id = extrair_asset_id(data)
        if asset_id and validar_asset_id(cfg, asset_id):
            return asset_id

        status = str(data.get("status", "")).lower()
        if status in ("done", "completed", "success", "succeeded"):
            print("⚠️ a operação terminou, mas sem asset válido.")
            return None

        time.sleep(POLL_INTERVAL_SECONDS)


def upload_decal_grupo(cfg: RobloxConfig, png: bytes) -> Optional[str]:
    if cfg.group_id <= 0:
        raise RuntimeError("groupId inválido")

    payload = {
        "assetType": "Decal",
        "displayName": f"GOES19_{datetime.now(timezone.utc).isoformat()}",
        "description": "GOES19 auto",
        "creationContext": {"creator": {"groupId": cfg.group_id}},
    }

    files = {
        "request": (None, json.dumps(payload), "application/json"),
        "fileContent": ("img.png", png, "image/png"),
    }

    response = _http_request(
        "POST",
        ROBLOX_UPLOAD_URL,
        headers=_headers(cfg),
        files=files,
    )

    data = response.json()
    op_id = data.get("operationId")

    if not op_id:
        print(f"⚠️ resposta sem operationId: {data}")
        return None

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


def main() -> None:
    try:
        cfg = build_config()

        raw = baixar_goes19()
        png = recorte_circular_png(raw)

        asset_id = upload_decal_grupo(cfg, png)

        if asset_id:
            with open(OUT_FILE, "w", encoding="utf-8") as f:
                f.write(asset_id)
            print("OK:", asset_id)
        else:
            print("⚠️ upload não finalizado dentro do tempo limite.")
    except Exception as exc:
        print(f"⚠️ execução encerrada com erro controlado: {exc}")


if __name__ == "__main__":
    main()
