# GranMago Scanner

Complemento de Home Assistant. Paper trading del scanner GranMago (EMA50 semanal / diario / 4H, estocástico y zona de liquidez).

El bucle corre **dentro del servidor**. Puedes cerrar la página o apagar la pantalla del móvil: las posiciones, el balance y el historial siguen en el addon.

No envía órdenes a ningún exchange. No es asesoramiento financiero.

## Instalar en Home Assistant

1. Ajustes → Complementos → Tienda de complementos → ⋮ → **Repositorios**.
2. Añade `https://github.com/iejayu8/granmago-scanner` y espera a que recargue.
3. Instala **GranMago Scanner**, arráncalo y activa "Iniciar con Home Assistant" si quieres que vuelva solo tras un reinicio.
4. Abre el panel **GranMago** en la barra lateral (o `http://<tu-home-assistant>:8787`).
5. Pulsa **Iniciar bot** una vez. A partir de ahí sigue solo.

El puerto 8787 no choca con HADomotics (8099).

## Qué hace

- Vigila cada posición abierta cada 20 segundos con el precio en vivo. El stop y el objetivo de una moneda no mueven los de otra.
- Escanea los pares de la opción `pairs` cada 90 segundos, también con posiciones abiertas. El defecto son los 28 pares de la monitorización: BTC, ETH, SOL, XRP, BNB, ADA, DOGE, AVAX, LINK, DOT, LTC, BCH, ZEC, PEPE, 1000BONK, NEAR, XMR, INJ, SUI, TAO, IOST, XLM, TRX, HBAR, ENA, WLD, AAVE y QNT.
- La estrategia es la de Gran Mago, variante A, estilo sweep, barridos y retrocesos, sin promediar. En cada escaneo automático abre la señal del cierre de 4H que todavía no se ha operado, hasta `max_positions` (3 por defecto). Una señal (par, dirección y cierre de esa vela) abre como máximo una vez: después del stop no se vuelve a entrar. La entrada consulta un ticker Blofin fresco justo antes del fill, no el cierre de una vela ni el precio guardado del escaneo. Si ese precio ya está en el stop o más allá, o ya ha llegado al objetivo, no se abre. Nunca dos posiciones en el mismo par, ni en el sentido contrario. El score se calcula para la tabla y no abre operaciones.
- Un escaneo manual actualiza el ranking y no abre operaciones.
- El trailing está apagado. Cada operación espera su stop, su objetivo, la salida por agotamiento o las 15 velas de confirmación. El botón **Cerrar** sigue cerrando a mercado.
- Guarda el estado en `/data/state.json` del addon. Un reinicio no borra el balance ni las operaciones, y si estaba en marcha se reanuda solo. Un estado de 1.2.0 con una sola `openPosition`, o un libro de 1.5.0, pasa a la lista al cargar: no se cierra y no se le cambia la entrada.

## Mercado exclusivo Blofin (1.6.2)

Velas, entradas, valoración/PnL y todos los cierres (SL, TP, manual, agotamiento,
tiempo, y los caminos opcionales de parcial/trailing/promedios) usan **solo
Blofin**, mismo instrumento USDT lineal SWAP. El precio observado es `last` del
ticker, no el endpoint distinto `mark-price`. No se consulta Binance ni otra
fuente y no se usa una vela/cotización guardada como sustituto de un ticker.

- La metadata del instrumento se comprueba en cada consulta: símbolo exacto,
  SWAP lineal, cotización/liquidación USDT y estado `live`. `reduce_only` permite
  cerrar, no abrir. No hay caché de cotizaciones ni de metadata.
- El `ts` del ticker es su **generación en milisegundos**, según la documentación
  pública de [Blofin](https://docs.blofin.com/index.html#public-data). Se acepta
  antigüedad máxima 30 s y adelanto de reloj máximo 2 s. `last`/bid/ask deben ser
  positivos, finitos y coherentes: spread <=5%, last dentro de bid/ask con margen
  5%. Se vuelve a validar la frescura bajo el lock antes de contabilizar.
- Las velas sirven para indicadores/señales, no como precio de ejecución. Se
  rechazan OHLC inválidos, duplicados o ventanas cuyo último inicio sea anterior
  a la duración de vela +60 s. La entrada exige escaneo de como máximo 180 s y
  divergencia ticker/precio de referencia <=5%; puede omitir señales en un salto
  brusco de mercado en lugar de inventar un fill. No se cambia la fórmula del
  riesgo, el stop, los objetivos o los indicadores.
- Si Blofin falla: **reintentos solo contra Blofin**, hasta 3 GET por endpoint,
  backoff 0,25/0,5 s ante errores de conexión/timeout y HTTP 408/429/500/502/503/504.
  Presupuesto de reintentos/lectura 8 s por consulta, compartido por metadata y
  ticker, con timeout de socket <=2 s. Se descartan respuestas que lleguen tarde
  y se corta la lectura incremental al agotar el presupuesto. No hay bucle de
  reintentos infinito ni reintento de una operación contable.
- Se espacian todos los GET >=0,25 s (hasta 240/min y 1200/5 min, por debajo de
  los límites publicados 500/min y 1500/5 min). `Retry-After` numérico o fecha HTTP
  se respeta con enfriamiento compartido; si excede el presupuesto se devuelve
  sin precio, sin dormir ese periodo largo ni cambiar de fuente. Un 403, JSON
  malformado, error semántico o dato inválido no se convierte en fill. El ciclo
  siguiente vuelve a intentarlo según la cadencia normal/enfriamiento.
- Hasta 4 posiciones se vigilan en paralelo, con sesión HTTP por hilo y sin
  mantener el lock del ledger durante la red. Se comprueba identidad de posición
  y escaneo para no cerrar una posición nueva con una respuesta vieja; ticks
  fuera de orden se descartan. Los reintentos GET no duplican entradas/cierres.
- Sin ticker válido no se abre, no se cierra ni se recalcula PnL. Se avisa en el
  log; el cierre manual devuelve 503 `sin_precio`. La interfaz muestra PnL/equity
  no disponibles cuando falta una valoración fresca. Esto significa que **el
  stop paper no está garantizado durante una caída de Blofin**; tras recuperarse
  puede haber un gap real y el fill seguirá siendo al ticker fresco, no al SL.
- Las operaciones nuevas guardan `entryMarket`/`lastMarket`; los cierres nuevos,
  `exitMarket` con proveedor, instrumento, timestamp y precio observado. El
  historial previo no se reescribe y no se corrige retroactivamente XMR.

### Pruebas y herramientas históricas

`python3 -m unittest discover -s tests -v` usa datos sintéticos y HTTP simulado,
con directorios temporales; no necesita HA, credenciales ni órdenes reales.
Los backtests siguen leyendo únicamente velas históricas Blofin y llaman
explícitamente al motor offline `_maybe_open_replay`. No disponen de snapshots
históricos de tickers/bid/ask y **no validan paridad exacta de fills en vivo** ni
la política de frescura de tickers. Sus cachés son históricas y nunca alimentan
las rutas de ejecución del complemento. No se ha recalculado rentabilidad con
este cambio.

## Robustez de mercado y demo Blofin (1.7.0)

- El precio de ejecución es el mid del libro (`books?size=1`) y su frescura es la del `ts` del libro (≤ 10 s). El `last` del ticker ya no se usa para decidir.
- La vela de 4H recién cerrada se acepta durante 180 s tras el cierre, y los pares que fallan se reintentan al final del escaneo.
- `/api/state` → `market` y `/api/health` muestran contadores por endpoint con el motivo exacto de cada rechazo y los pares degradados.
- `trading_mode: demo` ejecuta en Blofin Demo Trading con SL/TP en el exchange, reconciliación cada minuto y estado seguro ante cualquier duda. Guía completa: [DEMO_SETUP.md](DEMO_SETUP.md). El modo real está bloqueado.
- Los endpoints de control solo responden desde el panel de HA, loopback o con `control_token`.

## Riesgo con varias posiciones

Cada entrada arriesga `risk_pct` del **balance realizado** en ese momento. El tamaño no se reparte entre las posiciones: tres entradas pueden arriesgar 3 × `risk_pct`. Con el 1% por defecto, el riesgo a stop de un libro lleno es el 3% del balance.

El apalancamiento no multiplica el nocional. El nocional sale del riesgo y de la distancia al stop. Una **entrada nueva** se reduce al hueco que quede, o no se abre, cuando la suma de nocionales pasaría de balance × apalancamiento (3 por defecto). Las que ya están abiertas, incluida una posición migrada de una versión anterior, no se recortan ni se cierran por ese tope.

## Ajustes del complemento

| Opción | Por defecto | Uso |
|---|---|---|
| log_level | info | Log del complemento |
| initial_balance | 10000 | Balance inicial de paper, solo al resetear |
| risk_pct | 1 | % del balance arriesgado por operación |
| leverage | 3 | Tope de la suma de nocionales (balance × apalancamiento). El tamaño sale del riesgo y del stop |
| max_positions | 3 | Posiciones abiertas a la vez, una por moneda. De 1 a 12 |
| fee_pct_per_side | 0.06 | Comisión taker, en % del nocional, en la entrada y en la salida |
| slippage_pct_per_side | 0.02 | Empeora la entrada y la salida, en % del precio |
| pairs | los 28 de la monitorización | Swaps USDT a escanear. Una lista vacía o inválida usa esos 28 y sustituye la lista guardada en el estado. Máximo 40 |
| trading_mode | paper | `paper` (simulado), `demo` (Blofin Demo Trading, ver [DEMO_SETUP.md](DEMO_SETUP.md)) o `live` (bloqueado en esta versión) |
| blofin_api_key / blofin_api_secret / blofin_api_passphrase | vacío | Claves de la API key de **Demo Trading**. Solo se usan en `demo`; nunca aparecen en logs ni en la página |
| control_token | vacío | Opcional. Permite llamar a los endpoints de control desde fuera del panel con la cabecera `X-GranMago-Token` |
| kill_switch | false | Sin entradas nuevas. Las posiciones abiertas conservan su SL/TP |

El resto no está en la configuración. Queda fijo en la monitorización validada: Gran Mago, variante A, estilo sweep, setups both, sin promedios, salida por agotamiento, máximo 15 velas, margen de stop 0,3 %, objetivo 1,7 R en el camino del scanner, trailing apagado, filtro de régimen apagado, sin enfriamiento entre operaciones y un escaneo cada 90 segundos. Una actualización desde 1.5.0 ignora las claves viejas que siguieran guardadas.

## Riesgo, apalancamiento y el estado guardado

`risk_pct`, `leverage`, `max_positions`, las comisiones, el slippage y `pairs` del complemento se exportan al arrancar y **reemplazan** la copia guardada en `/data/state.json`. Hasta la 1.3.0 el estado ganaba en riesgo y apalancamiento, así que cambiar la opción no cambiaba el tamaño de las entradas si la página ya había guardado otro valor.

La página sigue pudiendo cambiar el riesgo y el apalancamiento mientras el complemento está en marcha. Esos cambios se guardan, y el siguiente arranque vuelve a aplicar las opciones del complemento. El intervalo de escaneo, la estrategia y el trailing no se cambian desde la página.

El Supervisor, al actualizar, solo escribe el valor por defecto de una clave que **falta**. Si en 1.5.0 ya tenías `risk_pct`, `max_positions`, las comisiones o `pairs`, se quedan como estaban. Los defectos nuevos (riesgo 1 %, 3 posiciones, comisión 0,06 %, slippage 0,02 % y los 28 pares) aplican en una instalación nueva y en una clave que nunca se guardó. Las claves que 1.5.1 quitó del esquema (`strategy`, `entry_threshold`, `trailing_*`, `regime_*`, `granmago_*`, …) no se leen. El arranque las borra de la configuración del complemento cuando la API del Supervisor responde, para que no quede el aviso de opción desconocida. Si no puede borrarlas, arranca igual.

El apalancamiento no multiplica el nocional de una entrada. Limita la suma de nocionales a balance × apalancamiento. El nocional de cada entrada sale del riesgo y de la distancia al stop.

## Costes, R y régimen

La comisión de entrada sale del balance al abrir. La de salida, y el slippage de ambos lados, entran en el PnL del historial. Con los defectos (0,06 % y 0,02 % por lado) el paper se acerca a una orden taker de Blofin. `rMultiple` es ese PnL neto partido por el riesgo inicial de esa parte.

El filtro de régimen está apagado y no se configura. La tendencia que usa el bot es la del modo Gran Mago, no la pendiente de la EMA50 del scanner.

## Modo Gran Mago

Es el único modo del complemento. No se elige en la configuración. Opera los dos lados y espera **dos cierres de 4H** (variante A). No se entra a mitad de vela ni en una vela posterior si la de confirmación ya pasó. El escaneo sigue calculando el score para la tabla, pero la orden sale de estas reglas.

La variante B (solo largos, un cierre de 12H) y el scanner por score siguen en el código de investigación. El complemento no los arranca. La vela de 12H, por si se lee el estudio, son tres velas de 4H de Blofin: 00, 04 y 08 UTC, o 12, 16 y 20 UTC.

La fase la marcan la EMA50 semanal y la diaria. Si no coinciden, manda la diaria. Un solo cierre al otro lado de la media no cambia la fase; hacen falta dos seguidos.

El setup A barre el mínimo o máximo diario importante (pivote de cierres, cinco velas a cada lado, el más extremo de los últimos 30 días que aún no se haya barrido) y espera los cierres de vuelta al nivel donde más mínimos o máximos de la vela de confirmación coinciden, con una tolerancia del 0,2 % y al menos tres toques. Si no hay ese racimo, el nivel es el propio pivote. La ventana es de 30 días porque 90 dejaba sin barrer el origen de la tendencia y ese mínimo viejo impedía cualquier barrido más cercano: el extremo que cuenta es el del rango que se ve. El setup B es el retroceso a favor de tendencia: la mecha toca la EMA50 diaria, un swing ya roto, un imbalance de tres velas o el 50 % de una mecha grande, y luego vienen los mismos cierres. Mientras el setup A está armado no se toma el B.

El estocástico es 14, 3, 3 y solo veta: no hay largo con %K por encima de 80 ni corto con %K por debajo de 20. La zona media no bloquea. El filtro de impulso aproxima el script «SQZ ADX» de TradingView, que no publica parámetros, con el Squeeze Momentum de LazyBear: Bandas de Bollinger 20 y 2, Canal de Keltner 20 y 1,5 veces la media simple del true range, histograma = regresión lineal de `cierre − media(media(máximo 20, mínimo 20), media del cierre 20)`. Para un largo hace falta el histograma negativo y subiendo, o cruzando por encima de cero, y el ADX(14) de Wilder subiendo respecto a la vela anterior (`adx > adx[1]`). El corto es el espejo. Esos indicadores se miran en la vela de confirmación (4H o 12H), no en diario.

El stop es un stop de precio más allá de la mecha barrida, a `max(0,5 × ATR de 14 velas de 4H, 0,3 % del precio)`. Si un imbalance sin rellenar o una mecha grande atraviesa ese margen, el stop pasa al otro lado del hueco. Por encima del 8 % del precio la operación no se abre. En X la invalidación a veces es el cierre al otro lado de la mecha: aquí ese cierre no añade promedios (el complemento no promedia); la posición espera el stop de precio, porque el bot vigila cada tick. El objetivo es el clúster del swing diario anterior en la dirección del beneficio, un 0,1 % por dentro del extremo. No hay objetivo en R ni un mínimo de beneficio/riesgo. Sus propias cifras (77 % de aciertos y profit factor 1,38) implican un beneficio medio de unas 0,4 veces la pérdida media, así que el filtro de 1,5 del apunte del curso no se aplica.

No hay trailing. La salida por agotamiento cierra cuando el estocástico está agotado y el histograma del squeeze pierde fuerza. A las 15 velas de confirmación se cierra por tiempo. El stop no se mueve a la entrada por un múltiplo de R.

`pairs` es la lista que recorre el escaneo. El defecto son los 28 pares de arriba. Un símbolo que no sea `BASE-USDT` se ignora. Si la opción llega vacía o no queda ninguno válido, se usan esos 28, también cuando `/data/state.json` todavía tiene los 12 pares de una versión anterior. El tope es 40. `max_positions` es el número de huecos (de 1 a 12), no el tamaño del universo. Quitar un par de la lista no cierra una posición que ya esté abierta: el bucle de 20 segundos la sigue vigilando. Si actualizas desde 1.5.0 y la opción `pairs` del complemento ya tenía símbolos, esa lista se conserva: el Supervisor no la reescribe. El defecto de 28 pares vale para una instalación nueva, para una opción que falte y para una lista vacía.

El estilo es el de barrido. El nivel barrido es el más extremo entre un pivote de cierre diario de los últimos 20 días (sigue siendo 5 y 5), un pivote de cierre de 4H de los últimos 30 bars de 4H, y el máximo o mínimo del día anterior. La confirmación es el cierre de vuelta al otro lado de ese precio, en las dos velas de 4H. Una vela que no recupera el nivel cancela el barrido; no se espera a una vela posterior. La tendencia es el último cierre diario contra la EMA50, sin semanal y sin las dos velas de histéresis. El estocástico confirma en vez de vetar: el largo necesita un cruce de %K por encima de 20 entre el barrido y la señal, y el corto un cruce por debajo de 80. El squeeze y el ADX se guardan en la señal (`sqzOk`, `adxOk`) y no impiden la entrada. El stop, el objetivo y el tope del 8 % son los de arriba. Se toman barridos y retrocesos.

No se promedia. La primera orden arriesga el `risk_pct` entero.

El filtro de noticias no está: el curso dice no operar con datos y en X dice que el IPC no le cambia la idea, y no hay un calendario fiable en el complemento. La contratendencia y el scalping de varios intentos tampoco se programan.

## Estrategia v7 (alternativa)

Se puede activar la lógica Pullback-to-EMA + RSI Recovery (15m) sustituyendo el modo scanner.
