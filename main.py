import os
import io
import time
import json
import requests
from datetime import datetime

from goes2go import GOES
from PIL import Image
from rembg import remove

# =========================
# CONFIGURAÇÕES DO DAVI
# =========================

ROBLOX_API_KEY = os.environ("ROBLOX_API_KEY")
USER_ID = 3538598020

DECAL_ID_TO_UPDATE = 79946879599509

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

HEADERS = {
    "x-api-key": ROBLOX_API_KEY,
}

# =========================
# FUNÇÕES
# =========================

def baixar_goes19_geocolor():
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
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        return r.content


def remover_fundo_ia(image_bytes):
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


def upload_decal_roblox(image_png_bytes):
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
                "userId": USER_ID
            }
        }
    }

    files = {
        "request": (None, json.dumps(payload), "application/json"),
        "fileContent": ("goes19.png", image_png_bytes, "image/png")
    }

    r = requests.post(ROBLOX_UPLOAD_URL, headers=HEADERS, files=files, timeout=120)
    print("Status:", r.status_code)
    if r.status_code not in (200, 201):
        print("Resposta:", r.text)
        raise Exception("Falha no upload do decal")

    data = r.json()
    # Normalmente vem algo como:
    # {"assetId":"123", "operationId":"..."}
    asset_id = data.get("assetId")
    operation_id = data.get("operationId")

    print("✅ Upload iniciado.")
    print("assetId:", asset_id)
    print("operationId:", operation_id)

    return asset_id, operation_id


def tentar_overwrite_decal(image_png_bytes):
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

    r = requests.patch(ROBLOX_UPDATE_URL, headers=HEADERS, files=files, timeout=120)

    print("Status:", r.status_code)
    print("Resposta:", r.text)

    if r.status_code not in (200, 201):
        print("⚠️ Não foi possível sobrescrever (provavelmente a API não permite overwrite nesse caso).")
        return False

    print("✅ Decal sobrescrito com sucesso!")
    return True


def main():
    if not ROBLOX_API_KEY:
        raise Exception("❌ ROBLOX_API_KEY não está definida. Coloque nos secrets/env.")

    print("=== GOES-19 -> Remover fundo -> Roblox Decal ===")

    # 1) baixar GOES
    img_bytes = baixar_goes19_geocolor()

    # 2) remover fundo
    png_bytes = remover_fundo_ia(img_bytes)

    # 3) tenta overwrite (se der)
    ok = tentar_overwrite_decal(png_bytes)

    # 4) se overwrite falhar, faz upload novo
    if not ok:
        asset_id, operation_id = upload_decal_roblox(png_bytes)
        print("\n✅ Novo decal criado!")
        print("➡️ Novo assetId:", asset_id)
        print("➡️ operationId:", operation_id)
        print("\nAgora você pode trocar o decal no seu jogo pra esse assetId.")

    print("\n✅ Finalizado!")


if __name__ == "__main__":
    main()
