import io
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

import requests
from PIL import Image, ImageDraw


# =========================
# CONFIG
# =========================

ROBLOX_API_KEY = os.environ.get("ROBLOX_API_KEY")
ROBLOX_USER_ID = int(os.environ.get("ROBLOX_USER_ID", "3538598020"))
ROBLOX_DECAL_ID = int(os.environ.get("ROBLOX_DECAL_ID", "79946879599509"))

MAX_SIZE = int(os.environ.get("MAX_SIZE", "1024"))  # 512/1024 recomendado
SLEEP_BETWEEN_RUNS = int(os.environ.get("SLEEP_SECONDS", "0"))

# CDN estável (GOES-19 GeoColor Full Disk)
GOES19_URL = os.environ.get(
    "GOES19_URL",
    "https://cdn.star.nesdis.noaa.gov/GOES19/ABI/FD/GEOCOLOR/latest.jpg"
)

DEFAULT_TIMEOUT = 120
DOWNLOAD_TIMEOUT = 60
MAX_RETRIES = 3

ROBLOX_UPLOAD_URL = "https://apis.roblox.com/assets/v1/assets"
ROBLOX_ASSET_URL = "https://apis.roblox.com/assets/v1/assets"
ROBLOX_OPERATIONS_URL = "https://apis.roblox.com/assets/v1/operations"


# =========================
# DATA
# =========================

@dataclass(frozen=True)
class RobloxConfig:
    api_key: str
    user_id: int
    decal_id: int


# =========================
# HTTP
# =========================

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
                method=method,
                url=url,
                headers=headers,
                files=files,
                timeout=timeout,
            )
            # Roblox às vezes responde 202/200. Só erro >=400 mesmo:
            if r.status_code >= 400:
                raise requests.HTTPError(f"{r.status_code}: {r.text}")

            return r

        except Exception as exc:
            last_error = exc
            print(f"⚠️ HTTP Falhou (tentativa {attempt}/{retries}): {exc}")
            if attempt < retries:
                time.sleep(2 * attempt)

    raise RuntimeError(f"Falha na requisição após {retries} tentativas.") from last_error


def _headers(cfg: RobloxConfig) -> dict:
    return {"x-api-key": cfg.api_key}


# =========================
# GOES
# =========================

def baixar_goes19() -> bytes:
    print("📡 Baixando imagem GOES-19 (latest.jpg)...")

    r = _http_request(
        "GET",
        GOES19_URL,
        headers={},
        timeout=DOWNLOAD_TIMEOUT,
        retries=MAX_RETRIES,
    )

    print(f"✅ GOES-19 baixado ({len(r.content)/1024:.1f} KB)")
    return r.content


# =========================
# IMAGE PROCESSING
# =========================

def recortar_planeta_para_png_alpha(image_bytes: bytes) -> bytes:
    """
    Converte JPG GOES em PNG com alpha, recortando circularmente o planeta.
    Sem IA. Sem bug.
    """
    print("🖼️ Processando imagem: recorte circular + alpha...")

    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    w, h = img.size
    print(f"📐 Original: {w}x{h}")

    # Assume que a Terra está no centro, recorta círculo central:
    size = min(w, h)
    left = (w - size) // 2
    top = (h - size) // 2
    img = img.crop((left, top, left + size, top + size))

    # máscara circular
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size - 1, size - 1), fill=255)

    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)

    # redimensiona se precisar
    if size > MAX_SIZE:
        out = out.resize((MAX_SIZE, MAX_SIZE), Image.LANCZOS)
        print(f"📏 Redimensionado -> {MAX_SIZE}x{MAX_SIZE}")

    # salva PNG
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# =========================
# ROBLOX
# =========================

def upload_decal(cfg: RobloxConfig, png_bytes: bytes) -> Tuple[Optional[str], Optional[str]]:
    print("☁️ Fazendo upload de decal novo no Roblox...")

    agora = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    payload = {
        "assetType": "Decal",
        "displayName": f"GOES19_{agora}",
        "description": "GOES-19 Full Disk (GeoColor) recorte circular automatico",
        "creationContext": {
            "creator": {
                "userId": cfg.user_id
            }
        }
    }

    files = {
        "request": (None, json.dumps(payload), "application/json"),
        "fileContent": ("goes19.png", png_bytes, "image/png")
    }

    r = _http_request(
        "POST",
        ROBLOX_UPLOAD_URL,
        headers=_headers(cfg),
        files=files,
        timeout=DEFAULT_TIMEOUT,
        retries=MAX_RETRIES,
    )

    data = r.json()
    asset_id = data.get("assetId")
    operation_id = data.get("operationId")

    print(f"✅ Upload iniciado | assetId={asset_id} | operationId={operation_id}")
    return asset_id, operation_id


def tentar_overwrite(cfg: RobloxConfig, png_bytes: bytes) -> Tuple[bool, Optional[str]]:
    """
    Tenta overwrite do asset existente (pode não funcionar dependendo das permissões).
    Retorna (ok, operationId)
    """
    print(f"♻️ Tentando overwrite do decal: {cfg.decal_id}")

    url = f"{ROBLOX_ASSET_URL}/{cfg.decal_id}"

    payload = {
        "assetType": "Decal",
        "displayName": f"GOES19_UPDATED_{datetime.now().strftime('%H-%M-%S')}",
        "description": "Overwrite automatico GOES-19",
    }

    files = {
        "request": (None, json.dumps(payload), "application/json"),
        "fileContent": ("goes19.png", png_bytes, "image/png")
    }

    try:
        r = _http_request(
            "PATCH",
            url,
            headers=_headers(cfg),
            files=files,
            timeout=DEFAULT_TIMEOUT,
            retries=MAX_RETRIES,
        )
        # pode vir 200/202 dependendo
        text = r.text
        print("Resposta overwrite:", text[:300])

        data = {}
        try:
            data = r.json()
        except Exception:
            pass

        operation_id = data.get("operationId")
        return True, operation_id

    except Exception as e:
        print("⚠️ Overwrite falhou (normal). Motivo:", e)
        return False, None


def esperar_operation(cfg: RobloxConfig, operation_id: Optional[str], timeout_sec: int = 180) -> bool:
    """
    Polling até a operação do Roblox completar.
    """
    if not operation_id:
        print("ℹ️ Sem operationId, pulando espera.")
        return True

    print("⏳ Esperando Roblox processar operação...")

    start = time.time()
    url = f"{ROBLOX_OPERATIONS_URL}/{operation_id}"

    while True:
        if time.time() - start > timeout_sec:
            print("⏱️ Timeout esperando operação.")
            return False

        r = _http_request(
            "GET",
            url,
            headers=_headers(cfg),
            timeout=DEFAULT_TIMEOUT,
            retries=MAX_RETRIES,
        )

        data = {}
        try:
            data = r.json()
        except Exception:
            print("⚠️ Não consegui ler JSON da operação, vou tentar de novo...")
            time.sleep(3)
            continue

        # status varia, então a gente imprime
        status = data.get("status") or data.get("done") or data.get("state")
        print("🔎 Operation status:", status)

        # muitos retornos possíveis, então considera concluído se achar "done"
        if str(status).lower() in ("done", "completed", "success", "succeeded", "true"):
            print("✅ Operação concluída!")
            return True

        # se já veio erro:
        if "error" in data:
            print("❌ Operação deu erro:", data["error"])
            return False

        time.sleep(3)


# =========================
# MAIN
# =========================

def _build_config() -> RobloxConfig:
    if not ROBLOX_API_KEY:
        raise RuntimeError("❌ ROBLOX_API_KEY não definido. Configure no GitHub Secrets.")
    return RobloxConfig(api_key=ROBLOX_API_KEY, user_id=ROBLOX_USER_ID, decal_id=ROBLOX_DECAL_ID)


def main():
    cfg = _build_config()
    print("=== GOES-19 -> Roblox Decal (v2.0) ===")

    goes_bytes = baixar_goes19()
    png_bytes = recortar_planeta_para_png_alpha(goes_bytes)

    ok, op = tentar_overwrite(cfg, png_bytes)

    if ok:
        esperar_operation(cfg, op)
        print("✅ Overwrite finalizado.")
    else:
        asset_id, operation_id = upload_decal(cfg, png_bytes)
        esperar_operation(cfg, operation_id)
        print("✅ Novo decal criado!")
        print("➡️ Novo assetId:", asset_id)

    if SLEEP_BETWEEN_RUNS > 0:
        print(f"😴 Dormindo {SLEEP_BETWEEN_RUNS}s...")
        time.sleep(SLEEP_BETWEEN_RUNS)

    print("✅ Finalizado!")


if __name__ == "__main__":
    main()
