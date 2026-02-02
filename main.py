import io
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

import requests

from goes2go import GOES
from PIL import Image
from rembg import remove

# =========================
# CONFIGURAÇÕES DO DAVI
# =========================

ROBLOX_API_KEY = os.environ.get("ROBLOX_API_KEY")
USER_ID = int(os.environ.get("ROBLOX_USER_ID", "3538598020"))

DECAL_ID_TO_UPDATE = int(os.environ.get("ROBLOX_DECAL_ID", "79946879599509"))

# Canal GOES (GeoColor é o mais bonito)
PRODUCT = "GeoColor"

# Se quiser reduzir tamanho (roblox não curte imagem gigante)
MAX_SIZE = 1024  # px (largura/altura)

# =========================
# ROBLOX ENDPOINTS
# =========================
ROBLOX_UPLOAD_URL = "https://apis.roblox.com/assets/v1/assets"
# Endpoint "update/overwrite" pode variar e pode não funcionar dependendo do tipo/perm:
ROBLOX_UPDATE_URL = f"https://apis.roblox.com/assets/v1/assets/{DECAL_ID_TO_UPDATE}"

DEFAULT_HEADERS = {"x-api-key": ROBLOX_API_KEY}
DEFAULT_TIMEOUT = 120
DOWNLOAD_TIMEOUT = 60
MAX_RETRIES = 3


@dataclass(frozen=True)
class RobloxConfig:
    api_key: str
    user_id: int
    decal_id: int


def _build_headers(config: RobloxConfig) -> dict:
    return {"x-api-key": config.api_key}


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
            response = requests.request(
                method, url, headers=headers, files=files, timeout=timeout
            )
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            print(f"⚠️ Falha na requisição (tentativa {attempt}/{retries}): {exc}")
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"Falha na requisição após {retries} tentativas.") from last_error

# =========================
# FUNÇÕES
# =========================

def baixar_goes19_geocolor() -> bytes:
    """
    Usa goes2go para baixar GOES-19 (ABI Full Disk GeoColor).
    Retorna bytes da imagem PNG/JPG.
    """
    print("📡 Baixando GOES-19 com goes2go...")

    # GOES 19 é o satélite mais novo.
    # A biblioteca goes2go normalmente usa GOES(satellite=18) etc.
    # Alguns builds suportam 19, outros ainda não.
    # Então aqui fazemos fallback: tenta 19, se falhar tenta buscar direto latest do CDN.
    try:
        G = GOES(satellite=19, product="ABI", domain="FD")
        # pega a lista e escolhe mais recente
        files = G.timerange(datetime.utcnow(), datetime.utcnow())
        latest = files[-1]
        img = G.image(latest, product=PRODUCT)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        print("⚠️ goes2go não conseguiu GOES-19 direto. Fazendo fallback via CDN.")
        print("Motivo:", e)

        # fallback (muito usado e funciona bem)
        # GOES-19 CDN da NOAA (se o caminho mudar você me fala que eu ajusto)
        url = "https://cdn.star.nesdis.noaa.gov/GOES19/ABI/FD/GEOCOLOR/latest.jpg"
        response = _http_request(
            "GET",
            url,
            headers={},
            timeout=DOWNLOAD_TIMEOUT,
            retries=MAX_RETRIES,
        )
        return response.content


def remover_fundo_ia(image_bytes: bytes) -> bytes:
    """
    Remove fundo com rembg.
    Retorna bytes PNG com alpha.
    """
    print("🧠 Removendo fundo com IA (rembg)...")

    # remove retorna bytes PNG com alpha
    out = remove(image_bytes)

    # abrir no PIL e redimensionar
    img = Image.open(io.BytesIO(out)).convert("RGBA")

    # reduzir imagem (roblox)
    w, h = img.size
    maior = max(w, h)
    if maior > MAX_SIZE:
        scale = MAX_SIZE / maior
        nw, nh = int(w * scale), int(h * scale)
        img = img.resize((nw, nh), Image.LANCZOS)
        print(f"📏 Redimensionado para {nw}x{nh}")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def upload_decal_roblox(
    config: RobloxConfig, image_png_bytes: bytes
) -> Tuple[Optional[str], Optional[str]]:
    """
    Faz upload como novo Decal.
    Retorna assetId do novo decal.
    """
    print("☁️ Fazendo upload da imagem no Roblox como Decal...")

    agora = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

    payload = {
        "assetType": "Decal",
        "displayName": f"GOES19_{agora}",
        "description": "Imagem GOES-19 (automática) com fundo removido",
        "creationContext": {
            "creator": {
                "userId": config.user_id
            }
        }
    }

    files = {
        "request": (None, json.dumps(payload), "application/json"),
        "fileContent": ("goes19.png", image_png_bytes, "image/png")
    }

    response = _http_request(
        "POST",
        ROBLOX_UPLOAD_URL,
        headers=_build_headers(config),
        files=files,
        timeout=DEFAULT_TIMEOUT,
        retries=MAX_RETRIES,
    )
    print("Status:", response.status_code)

    data = response.json()
    # Normalmente vem algo como:
    # {"assetId":"123", "operationId":"..."}
    asset_id = data.get("assetId")
    operation_id = data.get("operationId")

    print("✅ Upload iniciado.")
    print("assetId:", asset_id)
    print("operationId:", operation_id)

    return asset_id, operation_id


def tentar_overwrite_decal(config: RobloxConfig, image_png_bytes: bytes) -> bool:
    """
    Tenta atualizar o mesmo asset do decal.
    Pode falhar dependendo da permissão/API.
    """
    print("♻️ Tentando sobrescrever o decal existente (overwrite)...")

    payload = {
        "assetType": "Decal",
        "displayName": f"GOES19_UPDATED_{datetime.now().strftime('%H:%M:%S')}",
        "description": "Atualização automática GOES-19 (overwrite)",
    }

    files = {
        "request": (None, json.dumps(payload), "application/json"),
        "fileContent": ("goes19.png", image_png_bytes, "image/png")
    }

    try:
        response = _http_request(
            "PATCH",
            ROBLOX_UPDATE_URL,
            headers=_build_headers(config),
            files=files,
            timeout=DEFAULT_TIMEOUT,
            retries=MAX_RETRIES,
        )
        print("Status:", response.status_code)
        print("Resposta:", response.text)
    except RuntimeError:
        print("⚠️ Não foi possível sobrescrever (provavelmente a API não permite overwrite nesse caso).")
        return False

    print("✅ Decal sobrescrito com sucesso!")
    return True


def _build_config() -> RobloxConfig:
    if not ROBLOX_API_KEY:
        raise RuntimeError(
            "❌ ROBLOX_API_KEY não está definida. Coloque nos secrets/env."
        )
    return RobloxConfig(
        api_key=ROBLOX_API_KEY,
        user_id=USER_ID,
        decal_id=DECAL_ID_TO_UPDATE,
    )


def main() -> None:
    config = _build_config()

    print("=== GOES-19 -> Remover fundo -> Roblox Decal ===")

    # 1) baixar GOES
    img_bytes = baixar_goes19_geocolor()

    # 2) remover fundo
    png_bytes = remover_fundo_ia(img_bytes)

    # 3) tenta overwrite (se der)
    ok = tentar_overwrite_decal(config, png_bytes)

    # 4) se overwrite falhar, faz upload novo
    if not ok:
        asset_id, operation_id = upload_decal_roblox(config, png_bytes)
        print("\n✅ Novo decal criado!")
        print("➡️ Novo assetId:", asset_id)
        print("➡️ operationId:", operation_id)
        print("\nAgora você pode trocar o decal no seu jogo pra esse assetId.")

    print("\n✅ Finalizado!")


if __name__ == "__main__":
    main()
