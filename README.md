# Celerity Trader Bot

Bot de trading automatizado para **BTC/USDT** y **PAXG/USDT** (oro) en Binance.
Opera unicamente durante horario de Wall Street (NYSE: 9:30-16:00 ET).

## Setup rapido

```bash
# 1. Instalar dependencias
pip install -r requirements.txt

# 2. Configurar API keys de Binance (crear en https://www.binance.com/en/my/settings/api-management)
export BINANCE_API_KEY="tu_api_key"
export BINANCE_API_SECRET="tu_api_secret"

# 3. Ejecutar el bot
python app.py
```

Abrir **http://localhost:5000** en el navegador para ver el dashboard.

## Arquitectura

```
celerity-bot/
  config.py       -> Configuracion (pares, estrategia, horarios)
  strategy.py     -> Motor de estrategia (RSI + EMA + Volumen)
  trader.py       -> Conexion con Binance API (ordenes, balances)
  bot.py          -> Loop principal del bot
  app.py          -> Servidor web Flask + API REST
  templates/
    dashboard.html -> Dashboard de control
```

## Estrategia

Combina 3 indicadores tecnicos:

- **EMA Crossover** (9/21): detecta cambios de tendencia
- **RSI** (14): identifica sobrecompra/sobreventa
- **Volumen**: confirma que hay participacion del mercado

Senales:
- **BUY**: EMA bullish cross + RSI saludable + volumen alto
- **SELL**: EMA bearish cross O RSI overbought
- **Stop Loss**: -2% automatico
- **Take Profit**: +3% automatico

## Seguridad

- Arranca en modo **TESTNET** por defecto (dinero ficticio)
- Para operar en real, cambiar `testnet = False` en `config.py`
- Trades de $1-5 USD por operacion
- Maximo 2 posiciones abiertas simultaneas por par

## Dashboard

Interfaz web con:
- Controles Start/Stop/Pause
- Senales en tiempo real (RSI, EMA, volumen)
- Historial de trades con P&L
- Log de actividad
- Toggle para activar/desactivar pares
