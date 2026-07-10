# El Nuevo Vivir del Trading (The New Trading for a Living) — Dr. Alexander Elder
*Conocimiento destilado para agentes de trading. Tema: las 3 M (Mind, Method, Money) y sistema integrado.*

## Tesis central
El trading exitoso se apoya en tres pilares: **Mente** (psicología disciplinada), **Método** (sistema de análisis con ventaja) y **Money** (gestión de capital). Fallar en cualquiera de los tres destruye al trader.

## Money management — las reglas más citadas
- **Regla del 2%**: nunca arriesgar más del 2% del capital en un solo trade (riesgo = distancia al stop × tamaño). Protege contra el "mordisco de tiburón".
- **Regla del 6%**: si las pérdidas acumuladas del mes alcanzan el 6% del capital, dejar de operar hasta el mes siguiente. Protege contra la "mordedura de pirañas" (muchas pérdidas pequeñas).
- El tamaño de posición se deriva del riesgo, no al revés: `tamaño = (capital × %riesgo) / distancia_al_stop`.

## Sistema Triple Pantalla (Triple Screen)
Operar con tres "pantallas" (timeframes) en proporción ~5:1:
1. **Pantalla 1 — Marea** (timeframe largo, ej. semanal): determinar la tendencia con indicador de seguimiento (ej. pendiente de EMA, MACD histograma semanal). Solo se opera en su dirección.
2. **Pantalla 2 — Ola** (timeframe medio, ej. diario): esperar un retroceso contra la marea usando osciladores (Force Index, estocástico, RSI). En marea alcista, comprar cuando el oscilador marca sobreventa.
3. **Pantalla 3 — Ejecución**: entrada con stop de compra sobre el máximo previo (trailing buy-stop) o técnica equivalente intradía.

## Indicadores propios de Elder
- **Force Index**: volumen × cambio de precio. EMA-2 para timing de corto plazo, EMA-13 para fuerza de la tendencia intermedia.
- **Elder-Ray**: Bull Power (high − EMA13) y Bear Power (low − EMA13). Comprar en tendencia alcista cuando Bear Power es negativo pero subiendo.
- **Impulse System**: combina pendiente de EMA-13 + histograma MACD. Verde (ambos suben) = permitido comprar; rojo (ambos bajan) = permitido vender; azul = sin restricción. Funciona como *censor* que prohíbe operar contra el momentum.
- **Canales/envelopes**: targets de ganancia en el canal sobre la EMA; un buen trade compra cerca de la media y vende cerca del canal superior.

## Psicología (Mind)
- El mercado es una multitud; las emociones de la multitud crean tendencias y reversiones.
- El trader perdedor busca emoción; el profesional busca ejecución aburrida y consistente.
- Disciplina = seguir reglas escritas. Cada trade requiere razón de entrada, stop y objetivo documentados *antes* de entrar.

## Registros (parte esencial del método)
- Diario de trading con capturas de entrada y salida, calificación de cada trade.
- Calificar la calidad de ejecución: % del canal capturado (ej. capturar >30% del canal = excelente).
- La curva de equity es el indicador del trader mismo: si cae, reducir tamaño.

## Reglas operativas derivadas (para implementación en bot)
- **Hard cap de riesgo por trade**: 2% máximo (configurable más bajo); cálculo automático de tamaño desde la distancia al stop.
- **Hard cap mensual**: drawdown mensual ≥6% → bot pausado hasta el próximo período.
- **Arquitectura multi-timeframe**: señal solo válida si timeframe superior (tendencia), medio (retroceso/oscilador) y ejecución están alineados.
- **Censor de momentum (Impulse)**: bloquear longs cuando EMA y MACD-histograma caen juntos, y shorts cuando suben juntos.
- **Targets realistas**: objetivos en bandas/canales de volatilidad en vez de números redondos.
- **Equity curve monitoring**: reducir tamaño automáticamente cuando la curva de equity del bot cae bajo su propia media móvil.
