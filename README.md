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
- La estrategia es la de Gran Mago, variante A, estilo sweep, barridos y retrocesos, sin promediar. En cada escaneo automático abre la señal del cierre de vela que aún no tiene posición, hasta `max_positions` (3 por defecto). Nunca dos veces el mismo par, ni en el sentido contrario. El score se calcula para la tabla y no abre operaciones.
- Un escaneo manual actualiza el ranking y no abre operaciones.
- El trailing está apagado. Cada operación espera su stop, su objetivo, la salida por agotamiento o las 15 velas de confirmación. El botón **Cerrar** sigue cerrando a mercado.
- Guarda el estado en `/data/state.json` del addon. Un reinicio no borra el balance ni las operaciones, y si estaba en marcha se reanuda solo. Un estado de 1.2.0 con una sola `openPosition`, o un libro de 1.5.0, pasa a la lista al cargar: no se cierra y no se le cambia la entrada.

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
| pairs | los 28 de la monitorización | Swaps USDT a escanear. Vacío o inválido vuelve a esos 28. Máximo 40 |

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

`pairs` es la lista que recorre el escaneo. El defecto son los 28 pares de arriba. Un símbolo que no sea `BASE-USDT` se ignora. Si no queda ninguno válido se usan esos 28. El tope es 40. `max_positions` es el número de huecos (de 1 a 12), no el tamaño del universo. Quitar un par de la lista no cierra una posición que ya esté abierta: el bucle de 20 segundos la sigue vigilando. Si actualizas desde 1.5.0 y ya tenías una lista guardada, esa lista se conserva; el defecto de 28 pares es para una instalación nueva o para una opción `pairs` que falte.

El estilo es el de barrido. El nivel barrido es el más extremo entre un pivote de cierre diario de los últimos 20 días (sigue siendo 5 y 5), un pivote de cierre de 4H de los últimos 30 bars de 4H, y el máximo o mínimo del día anterior. La confirmación es el cierre de vuelta al otro lado de ese precio, en las dos velas de 4H. Una vela que no recupera el nivel cancela el barrido; no se espera a una vela posterior. La tendencia es el último cierre diario contra la EMA50, sin semanal y sin las dos velas de histéresis. El estocástico confirma en vez de vetar: el largo necesita un cruce de %K por encima de 20 entre el barrido y la señal, y el corto un cruce por debajo de 80. El squeeze y el ADX se guardan en la señal (`sqzOk`, `adxOk`) y no impiden la entrada. El stop, el objetivo y el tope del 8 % son los de arriba. Se toman barridos y retrocesos.

No se promedia. La primera orden arriesga el `risk_pct` entero.

El filtro de noticias no está: el curso dice no operar con datos y en X dice que el IPC no le cambia la idea, y no hay un calendario fiable en el complemento. La contratendencia y el scalping de varios intentos tampoco se programan.
