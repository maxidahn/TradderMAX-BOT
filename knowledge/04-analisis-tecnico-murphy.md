# Análisis Técnico de los Mercados Financieros — John J. Murphy
*Conocimiento destilado para agentes de trading. Tema: fundamentos completos de análisis técnico.*

## Premisas del análisis técnico
1. El mercado lo descuenta todo (el precio refleja toda la información).
2. Los precios se mueven en tendencias.
3. La historia se repite (patrones de comportamiento colectivo).

## Teoría de Dow (base)
- Tendencias primarias (meses/años), secundarias (semanas), menores (días).
- Tendencia alcista: máximos y mínimos crecientes. Bajista: decrecientes.
- El volumen debe confirmar la tendencia (expandirse en la dirección de la tendencia).
- Una tendencia sigue vigente hasta señal definitiva de reversión.

## Conceptos estructurales
- **Soporte/Resistencia**: zonas de concentración de oferta/demanda. Un soporte roto se convierte en resistencia y viceversa (cambio de polaridad). Relevancia proporcional a: número de toques, volumen, tiempo.
- **Líneas de tendencia y canales**: válidas con ≥2-3 toques; la ruptura con volumen señala posible cambio.
- **Retrocesos típicos**: 33%, 50%, 66% (y Fibonacci 38.2%, 61.8%).
- **Gaps**: de ruptura (inicio de movimiento), de continuación (mitad), de agotamiento (final).

## Patrones de cambio de tendencia
- Hombro-Cabeza-Hombro (y su inversa): objetivo = altura de la cabeza proyectada desde el neckline.
- Dobles/triples techos y suelos.
- Vueltas en un día / islas de reversión.
Requisito común: tendencia previa que revertir + confirmación por volumen + ruptura del nivel clave.

## Patrones de continuación
- Triángulos (simétrico, ascendente, descendente): ruptura ideal entre 1/2 y 3/4 del triángulo.
- Banderas y banderines: pausas breves con volumen decreciente; objetivo = mástil proyectado.
- Rectángulos y cuñas.

## Indicadores principales
- **Medias móviles**: SMA/EMA. Cruces (ej. 50/200 = golden/death cross). Funcionan en tendencia, fallan en rango. La media actúa como soporte/resistencia dinámica.
- **MACD**: cruce de línea y señal, divergencias con el precio, histograma como momentum.
- **RSI (14)**: sobrecompra >70, sobreventa <30. En tendencia fuerte permanece extremo (no contradecir la tendencia solo por RSI). Las divergencias son la señal más potente.
- **Estocástico**: %K/%D, útil en rangos.
- **Bandas de Bollinger**: contracción (squeeze) anticipa expansión de volatilidad; precio fuera de bandas = movimiento extremo, no señal automática de reversión.
- **ATR**: medida de volatilidad para stops y sizing.
- **Volumen y Open Interest** (futuros): volumen creciente confirma; precio sube + OI sube = tendencia sana; precio sube + OI baja = short covering, desconfiar.

## Marcos temporales múltiples
Analizar de mayor a menor: el timeframe superior define la tendencia y los niveles; el inferior define la ejecución (timing de entrada). Nunca operar contra la tendencia del timeframe superior sin razón explícita.

## Reglas operativas derivadas (para implementación en bot)
- **Confirmación múltiple**: una señal vale más si coinciden ≥2 evidencias independientes (ej. ruptura + volumen + momentum alineado).
- **Filtro de tendencia**: clasificar régimen (tendencia/rango) antes de elegir indicador — osciladores en rango, seguimiento de tendencia con medias en tendencia.
- **Divergencias como alerta**: divergencia RSI/MACD vs precio = reducir exposición o ajustar stops, no necesariamente revertir posición.
- **Stops basados en estructura**: colocar stops detrás de soportes/resistencias o por múltiplos de ATR, no por montos arbitrarios.
- **Objetivos medidos**: usar objetivos proyectados de patrones (altura del patrón) como targets de referencia.
- **Volumen como validador**: rupturas sin expansión de volumen tienen alta probabilidad de ser falsas.
