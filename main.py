import io
import json
import os
import time
import warnings
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import requests
from PIL import Image, ImageDraw

warnings.filterwarnings("ignore", category=Image.DecompressionBombWarning)


GOES19_URL = os.environ.get(
    "GOES19_URL",
    "https://cdn.star.nesdis.noaa.gov/GOES19/ABI/FD/GEOCOLOR/latest.jpg",
)

MAX_SIZE = int(os.environ.get("MAX_SIZE") or "1024")

ROBLOX_API_KEY = os.environ.get("ROBLOX_API_KEY")
ROBLOX_USER_ID = int(os.environ.get("ROBLOX_USER_ID") or "3538598020")

OUT_FILE = os.environ.get("OUT_FILE", "latest_decal_id.txt")

DEFAULT_TIMEOUT = 120
DOWNLOAD_TIMEOUT = 60
MAX_RETRIES = 3

ROBLOX_UPLOAD_URL = "https://apis.roblox.com/assets/v1/assets"
ROBLOX_OPERATIONS_URL = "https://apis.roblox.com/assets/v1/operations"

# ⚠️ esse endpoint pode não funcionar pra decal (depende da API/permissões)
ROBLOX_DELETE_ASSET_URL = "https://apis.roblox.com/assets/v1/assets"


@dataclass(frozen=True)
class RobloxConfig:
    api_key: str
    user_id: int


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
            r = requests.request(method, url, headers=headers, files=files, timeout=timeout)
            if r.status_code >= 400:
                raise requests.HTTPError(f"{r.status_code}: {r.text}")
            return r
        except Exception as exc:
            last_error = exc
            print(f"⚠️ HTTP Falhou (tentativa {attempt}/{retries}): {exc}")
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"Falha na requisição após {retries} tentativas.") from last_error


def baixar_goes19() -> bytes:
    print("📡 Baixando imagem GOES-19...")
    r = _http_request("GET", GOES19_URL, headers={}, timeout=DOWNLOAD_TIMEOUT)
    print(f"✅ Baixado ({len(r.content)/1024:.1f} KB)")
    return r.content


def recorte_circular_png(image_bytes: bytes) -> bytes:
    print("🖼️ Recorte circular + alpha...")

    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    w, h = img.size
    print(f"📐 Original: {w}x{h}")

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
        print(f"📏 Redimensionado -> {MAX_SIZE}x{MAX_SIZE}")

    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def esperar_operation_e_pegar_asset_id(cfg: RobloxConfig, operation_id: str, timeout_sec: int = 240) -> str:
    print("⏳ Esperando operação...")

    start = time.time()
    url = f"{ROBLOX_OPERATIONS_URL}/{operation_id}"

    while True:
        if time.time() - start > timeout_sec:
            raise RuntimeError("⏱️ Timeout esperando operação.")

        r = _http_request("GET", url, headers=_headers(cfg))
        data = r.json()

        status = data.get("status") or data.get("done") or data.get("state")
        print("🔎 status:", status)

        # tenta achar assetId onde quer que esteja
        asset_id = (
            data.get("assetId")
            or (data.get("response") or {}).get("assetId")
            or (data.get("result") or {}).get("assetId")
            or (data.get("metadata") or {}).get("assetId")
        )

        if asset_id:
            return str(asset_id)

        if "error" in data:
            raise RuntimeError(f"❌ Operação com erro: {data['error']}")

        if str(status).lower() in ("done", "completed", "success", "succeeded", "true"):
            raise RuntimeError(f"❌ Terminou sem assetId. JSON={data}")

        time.sleep(3)


def upload_decal(cfg: RobloxConfig, png_bytes: bytes) -> str:
    print("☁️ Upload decal novo...")

    agora = datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")
    payload = {
        "assetType": "Decal",
        "displayName": f"GOES19_{agora}",
        "description": "GOES-19 GeoColor recorte circular automatico",
        "creationContext": {"creator": {"userId": cfg.user_id}},
    }

    files = {
        "request": (None, json.dumps(payload), "application/json"),
        "fileContent": ("goes19.png", png_bytes, "image/png"),
    }

    r = _http_request("POST", ROBLOX_UPLOAD_URL, headers=_headers(cfg), files=files)
    data = r.json()

    operation_id = data.get("operationId")
    if not operation_id:
        raise RuntimeError(f"❌ Upload não retornou operationId. Resposta: {data}")

    return esperar_operation_e_pegar_asset_id(cfg, operation_id)


def ler_id_antigo(path: str) -> Optional[str]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            old_id = f.read().strip()
        return old_id or None
    except Exception:
        return None


def salvar_asset_id(asset_id: str, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(asset_id.strip() + "\n")
    print(f"💾 Atualizei {path}: {asset_id}")


def tentar_deletar_asset(cfg: RobloxConfig, asset_id: str) -> bool:
    """
    Tenta deletar o asset antigo.
    Pode falhar pois Roblox nem sempre permite delete via Open Cloud.
    """
    asset_id = (asset_id or "").strip()
    if not asset_id:
        return False

    print(f"🗑️ Tentando deletar asset antigo: {asset_id}")

    url = f"{ROBLOX_DELETE_ASSET_URL}/{asset_id}"

    try:
        r = _http_request("DELETE", url, headers=_headers(cfg), timeout=DEFAULT_TIMEOUT, retries=1)
        print("✅ Delete OK:", r.status_code)
        return True
    except Exception as e:
        print("⚠️ Não consegui deletar (normal):", e)
        return False


def build_config() -> RobloxConfig:
    if not ROBLOX_API_KEY:
        raise RuntimeError("❌ ROBLOX_API_KEY não definido.")
    return RobloxConfig(api_key=ROBLOX_API_KEY, user_id=ROBLOX_USER_ID)


def main():
    print("=== GOES-19 -> Roblox Decal (upload + delete antigo) ===")
    cfg = build_config()

    old_id = ler_id_antigo(OUT_FILE)
    if old_id:
        print("📌 ID antigo:", old_id)
    else:
        print("📌 Sem ID antigo ainda.")

    goes_bytes = baixar_goes19()
    png_bytes = recorte_circular_png(goes_bytes)

    new_id = upload_decal(cfg, png_bytes)
    print("🆕 Novo assetId:", new_id)

    salvar_asset_id(new_id, OUT_FILE)

    # tenta deletar o antigo depois de salvar o novo (mais seguro)
    if old_id and old_id != new_id:
        tentar_deletar_asset(cfg, old_id)

    print("✅ Finalizado!")


if __name__ == "__main__":
    main()
