# La Metodología Wyckoff en Profundidad — Rubén Villahermosa
*Conocimiento destilado para agentes de trading. Tema: estructura de mercado, acumulación/distribución, oferta y demanda.*

## Tesis central
El precio se mueve por desequilibrios entre oferta y demanda generados por el "dinero profesional" (Composite Man). Los grandes operadores acumulan (compran) en rangos antes de subidas y distribuyen (venden) en rangos antes de caídas. Leer la huella de ese proceso — precio + volumen + estructura — permite posicionarse con ellos.

## Las 3 leyes fundamentales
1. **Oferta y demanda**: precio sube cuando demanda > oferta y viceversa. El volumen revela la presencia de interés profesional.
2. **Causa y efecto**: la magnitud de un movimiento (efecto) es proporcional a la preparación previa (causa = tiempo/tamaño del rango de acumulación o distribución).
3. **Esfuerzo vs. resultado**: volumen (esfuerzo) debe corresponderse con movimiento de precio (resultado). Divergencia = posible giro (ej. volumen alto sin progreso de precio = absorción).

## El ciclo del precio
Acumulación → Tendencia alcista (markup) → Distribución → Tendencia bajista (markdown). Reacumulación y redistribución son pausas dentro de tendencias.

## Estructura de acumulación (eventos, en orden)
- **PS** (Preliminary Support): primeras compras grandes que frenan la caída.
- **SC** (Selling Climax): pánico vendedor con volumen extremo; los profesionales absorben.
- **AR** (Automatic Rally): rebote que define el techo del rango.
- **ST** (Secondary Test): retest de la zona del SC con menos volumen (sequía de oferta).
- **Spring / Shakeout**: penetración falsa bajo el rango que captura stops y testea la oferta restante. Evento clave: si el volumen en el spring es bajo y la recuperación rápida, la oferta se agotó.
- **Test del spring**: confirmación con volumen menguante.
- **SOS** (Sign of Strength) / **JAC** (Jump Across the Creek): ruptura alcista del rango con volumen y velas amplias.
- **LPS / BU** (Last Point of Support / Back-Up): retroceso a la zona de ruptura — entrada de mejor ratio riesgo/beneficio.

## Estructura de distribución (espejo)
PSY, **BC** (Buying Climax), AR, ST, **UTAD** (Upthrust After Distribution: falsa ruptura sobre el rango), **SOW** (Sign of Weakness), **LPSY** (Last Point of Supply). El UTAD es el equivalente bajista del spring.

## Fases del rango (A-E)
- **A**: parada de la tendencia previa (PS, SC, AR, ST).
- **B**: construcción de la causa (oscilación dentro del rango).
- **C**: test definitivo (spring/UTAD) — la trampa.
- **D**: tendencia dentro del rango hacia la ruptura (SOS/SOW, LPS/LPSY).
- **E**: tendencia fuera del rango.

## Lectura de velas con volumen (VSA integrado)
- Volumen alto + rango estrecho + cierre medio = absorción (posible giro).
- Ruptura con volumen bajo = sospechosa de falsa.
- "No Demand" (vela alcista con volumen bajo en tendencia bajista) = debilidad.
- Clímax: volumen extremo tras movimiento extendido = probable final de tramo.

## Zonas operativas (las 3 oportunidades)
1. En la fase C: comprar el spring / vender el UTAD (mejor precio, más riesgo de fallo).
2. En la fase D: comprar el SOS o su test (confirmación).
3. En la fase E / LPS-BU: comprar el retroceso post-ruptura (más confirmación, peor precio).

## Reglas operativas derivadas (para implementación en bot)
- **Detección de rango**: identificar consolidaciones (compresión de volatilidad, oscilación entre extremos definidos) como candidatas a acumulación/distribución.
- **Clasificador de eventos**: detectar clímax (volumen percentil extremo + rango amplio), tests (retest con volumen decreciente) y falsas rupturas (penetración del extremo + reingreso rápido al rango).
- **Señal tipo spring**: ruptura bajo soporte del rango + recuperación dentro del rango en N velas + volumen no expandiéndose en la ruptura → señal larga con stop bajo el mínimo del spring.
- **Confirmación esfuerzo/resultado**: validar rupturas exigiendo volumen > media y desplazamiento real de precio; penalizar rupturas con divergencia esfuerzo-resultado.
- **Proporcionalidad causa-efecto**: targets proporcionales al tamaño/duración del rango previo.
- **Contexto jerárquico**: estructura del timeframe mayor manda; operar springs de timeframe menor solo a favor de la fase del timeframe mayor.
- **Stops estructurales**: siempre detrás del evento (mínimo del spring, máximo del UTAD), nunca dentro del rango.
