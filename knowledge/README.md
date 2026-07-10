# Knowledge Base — Manuales de Trading

Conocimiento destilado de 6 libros clásicos de trading, escrito como referencia operativa para los agentes del bot. Cada documento termina con una sección de **reglas operativas derivadas** pensadas para implementarse en código.

| Doc | Libro | Autor | Tema principal |
|---|---|---|---|
| [01](01-trading-en-la-zona-douglas.md) | Trading en la Zona | Mark Douglas | Psicología, pensar en probabilidades, consistencia |
| [02](02-entrenador-de-trading-steenbarger.md) | El Entrenador de Trading | Brett Steenbarger | Proceso, journaling, métricas, mejora continua |
| [03](03-el-mejor-perdedor-gana-hougaard.md) | El Mejor Perdedor Gana | Tom Hougaard | Gestión de pérdidas, asimetría, anti-martingala |
| [04](04-analisis-tecnico-murphy.md) | Análisis Técnico de los Mercados Financieros | John J. Murphy | Tendencias, patrones, indicadores, volumen |
| [05](05-vivir-del-trading-elder.md) | El Nuevo Vivir del Trading | Alexander Elder | Las 3 M, regla 2%/6%, Triple Pantalla |
| [06](06-metodologia-wyckoff-villahermosa.md) | La Metodología Wyckoff en Profundidad | Rubén Villahermosa | Acumulación/distribución, springs, esfuerzo vs. resultado |

## Cómo usarlo con los agentes
Cargar estos archivos como contexto/system prompt de los agentes (ej. en `claude_agent.py` o `agents/`), o indexarlos para retrieval. Las secciones "Reglas operativas derivadas" son directamente traducibles a lógica de `strategy.py`, gestión de riesgo y circuit breakers.

## Síntesis transversal (consenso de los 6 libros)
1. Riesgo predefinido y limitado por trade (≤2%) y por período (regla 6% de Elder).
2. Cortar pérdidas rápido, dejar correr ganancias; nunca promediar en pérdida.
3. Operar a favor de la tendencia del timeframe superior.
4. El volumen valida; las rupturas sin volumen son sospechosas.
5. Evaluar por series de trades y por proceso, no por resultados individuales.
6. Circuit breakers automáticos ante drawdown o rachas perdedoras.

*Nota: estos documentos son síntesis originales de los conceptos de cada obra, no reproducciones del texto. Para el contenido completo, comprar los libros (Amazon/Kindle, Google Play Books, editoriales).*
