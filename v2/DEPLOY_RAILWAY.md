# Celerity v2 → Railway (con GitHub `TradderMAX-BOT` + Claude Code)

Objetivo: subir el bot a tu repo **`maxidahn/TradderMAX-BOT`** y dejarlo corriendo
24/7 en Railway, en **paper**, con el dashboard accesible por una URL.

⚠️ Ojo con un detalle de tu setup: tu carpeta local hoy está conectada a otro repo
(`maxidahn/max.git`). Vamos a reapuntarla a `TradderMAX-BOT` antes de subir.

Lo que ya dejé listo en `v2/`: `Procfile`, `railway.toml`, `requirements.txt`,
`runtime.txt`, `.gitignore` (no sube `.env` ni el `venv`), y `run_local.py`
escuchando en `0.0.0.0:$PORT` con dashboard protegible por token.

---

## Parte 1 — Subir el código a GitHub (con Claude Code)

Claude Code es el agente de terminal; hace el git y el push por vos.

### 1. Instalar Claude Code (una vez)
```
npm install -g @anthropic-ai/claude-code
```
Si no tenés Node.js, instalalo antes desde https://nodejs.org (botón LTS).

### 2. Abrir el proyecto
```
cd /Users/max/Documents/Celerity/Celerity-Trader/celerity-bot
claude
```
La primera vez te pide iniciar sesión; podés entrar con tu **suscripción de Claude**.

### 3. Pegarle esta instrucción a Claude Code
Copiá y pegá tal cual:

> Mi carpeta local está conectada al repo git `maxidahn/max.git`, pero quiero
> pasarla a `https://github.com/maxidahn/TradderMAX-BOT.git`. Necesito que:
> 1. Verifiques que `.env` NO esté trackeado (no debe subirse nunca).
> 2. Reapuntes el remote `origin` a `https://github.com/maxidahn/TradderMAX-BOT.git`.
> 3. Hagas commit de todos los cambios, incluida la carpeta `v2/`, con un mensaje
>    claro tipo "Add Celerity v2 (bot local + dashboard + deploy Railway)".
> 4. Hagas `push` a la rama `main` de ese repo. Si el repo remoto ya tiene commits
>    y hay conflicto, avisame antes de forzar nada.
> Después confirmame que `v2/` quedó subido en TradderMAX-BOT.

Claude Code te va pidiendo confirmación en los pasos sensibles.

> **Alternativa manual** (si no querés usar Claude Code), en la Terminal:
> ```
> cd /Users/max/Documents/Celerity/Celerity-Trader/celerity-bot
> git remote set-url origin https://github.com/maxidahn/TradderMAX-BOT.git
> git add -A
> git commit -m "Add Celerity v2 (bot local + dashboard + deploy Railway)"
> git push -u origin main
> ```

---

## Parte 2 — Conectar Railway al repo (unos clics en la web)

Esta parte es en el navegador (Railway no la hace por CLI):

1. Entrá a https://railway.app → **New Project** → **Deploy from GitHub repo**.
2. Autorizá GitHub si te lo pide y elegí **`TradderMAX-BOT`**.
3. Cuando cree el servicio, andá a **Settings → Root Directory** y poné: `v2`
   (esto es clave: le dice a Railway que el bot vive en la subcarpeta `v2/`).
4. En **Variables**, agregá:
   - `V2_PAPER` = `true`
   - `V2_SYMBOLS` = `BTCUSDT,ETHUSDT`
   - `V2_TIMEFRAME` = `4h`
   - `V2_DASHBOARD_TOKEN` = (algo largo y único, ej. `celerity-9f3k2xQ...`)
5. **Settings → Networking → Generate Domain** para obtener la URL pública.
6. Entrá al dashboard en `https://TU-URL.up.railway.app/?token=EL_TOKEN`.

A partir de ahí, cada vez que subas cambios a GitHub (`git push`), Railway
redespliega solo.

---

## Variables de entorno (resumen)

| Variable | Valor | Para qué |
|---|---|---|
| `V2_PAPER` | `true` | Simula, no toca dinero. **Dejalo en true.** |
| `V2_SYMBOLS` | `BTCUSDT,ETHUSDT` | Pares que opera |
| `V2_TIMEFRAME` | `4h` | Temporalidad de decisión |
| `V2_DASHBOARD_TOKEN` | (algo largo) | Protege el dashboard público |
| `PORT` | (lo pone Railway) | No lo toques |
| `DATA_DIR` | `/data` + Volume | Opcional: historial que sobrevive redeploys |

En paper **no** necesitás claves de Binance (usa datos públicos). Recién cuando
quieras pasar a real cargás `BINANCE_API_KEY` / `BINANCE_API_SECRET` (permiso
Futures + Trading, **nunca** retiro) y cambiás `V2_PAPER=false`.

## Persistencia (opcional pero recomendado)
El disco de Railway se borra en cada redeploy. Para conservar el historial: en el
servicio → **Volumes** → montá uno en `/data`, y seteá `DATA_DIR=/data`.

## Después del deploy
- Entrá al dashboard con tu token: debe decir **PAPER** y equity $500.
- Dejalo correr. En 4h vas a ver pocas señales — es esperado.
- A las ~4 semanas, si la expectativa es positiva, evaluamos pasar a real.
