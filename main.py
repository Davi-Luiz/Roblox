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
import re
from typing import Any, Optional

warnings.filterwarnings("ignore", category=Image.DecompressionBombWarning)

# =========================
# CONFIG
# =========================

GOES19_URL = os.environ.get(
    "GOES19_URL",
    "https://cdn.star.nesdis.noaa.gov/GOES19/ABI/FD/GEOCOLOR/latest.jpg",
)

MAX_SIZE = int(os.environ.get("MAX_SIZE") or "1024")

ROBLOX_API_KEY = os.environ.get("ROBLOX_API_KEY")
ROBLOX_USER_ID = int(os.environ.get("ROBLOX_USER_ID") or "3538598020")

# ESSENCIAL PARA JOGO DE GRUPO:
ROBLOX_GROUP_ID = int(os.environ.get("ROBLOX_GROUP_ID") or "0")

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

def extrair_asset_id(obj: Any) -> Optional[str]:
    """
    Tenta encontrar o assetId verdadeiro em formatos comuns:
    - assetId numérico (int/str)
    - response.path = "assets/<id>" ou "assets/<id>/versions/<n>"
    - path = "assets/<id>..."
    Retorna o ID como string, ou None.
    """
    # 1) assetId direto
    for key in ("assetId", "asset_id", "assetID"):
        if isinstance(obj, dict) and key in obj:
            v = obj.get(key)
            if isinstance(v, int):
                return str(v)
            if isinstance(v, str) and v.strip().isdigit():
                return v.strip()

    # 2) procurar em campos de "path"
    candidates = []
    if isinstance(obj, dict):
        for key in ("path",):
            if isinstance(obj.get(key), str):
                candidates.append(obj[key])
        # response/result/metadata podem conter path também
        for key in ("response", "result", "metadata"):
            sub = obj.get(key)
            if isinstance(sub, dict) and isinstance(sub.get("path"), str):
                candidates.append(sub["path"])

    # Extrai assets/<digits> de qualquer candidate
    for s in candidates:
        m = re.search(r"\bassets/(\d+)\b", s)
        if m:
            return m.group(1)

    return None
def validar_asset_id(cfg: RobloxConfig, asset_id: str) -> bool:
    """
    Confere no endpoint de Asset se o ID é válido.
    Se der 200 e o path bater com 'assets/<id>', está ok.
    """
    url = f"{ROBLOX_UPLOAD_URL}/{asset_id}"  # /assets/v1/assets/{assetId}
    r = _http_request("GET", url, headers=_headers(cfg))
    data = r.json()
    path = (data.get("path") or "").strip()
    return path.startswith(f"assets/{asset_id}")


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


# =========================
# GOES
# =========================

def baixar_goes19() -> bytes:
    print("📡 Baixando imagem GOES-19...")
    r = _http_request("GET", GOES19_URL, headers={}, timeout=DOWNLOAD_TIMEOUT)
    print(f"✅ Baixado ({len(r.content)/1024:.1f} KB)")
    return r.content


# =========================
# IMAGE
# =========================

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


# =========================
# ROBLOX
# =========================

def esperar_operation_e_pegar_asset_id(cfg: RobloxConfig, operation_id: str, timeout_sec: int = 240) -> str:
    print("⏳ Esperando Roblox processar operação...")

    start = time.time()
    url = f"{ROBLOX_OPERATIONS_URL}/{operation_id}"

    last_json = None

    while True:
        if time.time() - start > timeout_sec:
            raise RuntimeError(f"⏱️ Timeout esperando operação do Roblox. Ultimo JSON={last_json}")

        r = _http_request("GET", url, headers=_headers(cfg))
        data = r.json()
        last_json = data

        status = data.get("status") or data.get("done") or data.get("state")
        print("🔎 Operation status:", status)

        # ✅ pega o assetId “de verdade”, mesmo se vier em response.path
        asset_id = extrair_asset_id(data)

        if asset_id:
            # ✅ valida antes de salvar (evita gravar ID errado)
            if validar_asset_id(cfg, asset_id):
                return asset_id
            else:
                print(f"⚠️ Achei um assetId ({asset_id}) mas nao validou ainda. Vou continuar esperando...")

        if "error" in data:
            raise RuntimeError(f"❌ Operação retornou erro: {data['error']}")

        # Se marcou como concluído mas ainda não achou asset válido:
        if str(status).lower() in ("done", "completed", "success", "succeeded", "true"):
            raise RuntimeError(f"❌ Operação terminou mas não encontrei assetId válido. JSON={data}")

        time.sleep(3)


def upload_decal_grupo(cfg: RobloxConfig, png_bytes: bytes) -> str:
    print("☁️ Upload decal novo (GRUPO)...")

    if cfg.group_id <= 0:
        raise RuntimeError("❌ ROBLOX_GROUP_ID inválido. Configure para o ID do grupo.")

    agora = datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")

    payload = {
        "assetType": "Decal",
        "displayName": f"GOES19_{agora}",
        "description": "GOES-19 GeoColor recorte circular automatico",
        "creationContext": {
            "creator": {
                "groupId": cfg.group_id
            }
        },
    }

    files = {
        "request": (None, json.dumps(payload), "application/json"),
        "fileContent": ("goes19.png", png_bytes, "image/png"),
    }

    r = _http_request("POST", ROBLOX_UPLOAD_URL, headers=_headers(cfg), files=files)
    data = r.json()

    operation_id = data.get("operationId")
    asset_id_early = data.get("assetId")

    print("operationId:", operation_id)
    print("assetId (se vier):", asset_id_early)

    if not operation_id:
        raise RuntimeError(f"❌ Upload não retornou operationId. Resposta: {data}")

    return esperar_operation_e_pegar_asset_id(cfg, operation_id)


def salvar_asset_id(asset_id: str, path: str) -> None:
    asset_id = (asset_id or "").strip()
    if not asset_id:
        raise RuntimeError("❌ assetId veio vazio.")
    with open(path, "w", encoding="utf-8") as f:
        f.write(asset_id + "\n")
    print(f"💾 latest saved: {path} -> {asset_id}")


# =========================
# MAIN
# =========================

def build_config() -> RobloxConfig:
    if not ROBLOX_API_KEY:
        raise RuntimeError("❌ ROBLOX_API_KEY não definido (Secrets).")
    if ROBLOX_GROUP_ID <= 0:
        raise RuntimeError("❌ ROBLOX_GROUP_ID não definido / inválido (Vars).")

    return RobloxConfig(
        api_key=ROBLOX_API_KEY,
        user_id=ROBLOX_USER_ID,
        group_id=ROBLOX_GROUP_ID,
    )


def main():
    print("=== GOES-19 -> Roblox Decal (GROUP UPLOAD) ===")

    cfg = build_config()
    print("GroupId:", cfg.group_id)

    goes = baixar_goes19()
    png = recorte_circular_png(goes)

    asset_id = upload_decal_grupo(cfg, png)
    print("🆕 Novo assetId:", asset_id)

    salvar_asset_id(asset_id, OUT_FILE)

    print("✅ Finalizado!")


if __name__ == "__main__":
    main()
