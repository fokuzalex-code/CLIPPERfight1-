# Clipper.fight 🥊
App web mobile-first que convierte streams de Kick o videos locales en clips verticales 9:16.

## Local
    pip install -r requirements.txt   # (opcional) pip install -r requirements-optional.txt  -> MediaPipe
    # requiere ffmpeg instalado en el sistema
    uvicorn app:app --host 0.0.0.0 --port 8000
Abre `http://IP-DE-TU-PC:8000` desde el celular (misma red WiFi).

## Render
1. Sube esta carpeta a un repo de GitHub.
2. Render → New → Blueprint (usa `render.yaml`) o Web Service con runtime **Docker**.
3. Plan con **≥ 2 GB de RAM** y un Disk montado en `/data` (los discos requieren plan de pago).
4. Variable `ACCESS_KEY`: tu contraseña. Entra desde el celular a `https://TU-APP.onrender.com/login?key=TU_CLAVE` una sola vez.

## Notas
- Whisper corre en CPU: usa `tiny`/`base` en Render. Un stream de 1 h puede tardar varios minutos.
- Kick puede bloquear descargas desde IPs de datacenter (Cloudflare). Si falla la URL, sube el archivo.
- Chat overlay: pendiente (Kick no expone el chat histórico de forma estable).
