# Changelog

## 1.7.3

- **Panel demo: saldo libre y margen en uso.** En demo la tarjeta "Balance (Blofin demo)" mostraba `details[USDT].balance` ("Cash balance"), que excluye el margen aislado de las posiciones abiertas: 838 junto a una Equity de 1000,43 parecía dinero perdido. Ahora:
  - La tarjeta se llama **"Saldo libre (fuera de posiciones)"** y muestra `details[USDT].available` ("Available balance" en la doc de Blofin), con 2 decimales. Si Blofin no lo envía, usa `balance`.
  - Nueva tarjeta **"Margen en uso"** (solo en demo). Es la suma del campo `margin` de cada posición en `GET /api/v1/account/positions` (`initialMargin` en las de margen cruzado), leída en la misma reconciliación que el saldo. Si las posiciones no se pueden leer, se usa `details[USDT].isolatedEquity − isolatedUnrealizedPnl`.
  - Las dos tarjetas llevan un tooltip: "Equity = saldo libre + margen en uso + PnL no realizado (Blofin)".
  - La Equity sigue siendo `totalEquity`. Paper no cambia.
- `exchangeAccount` guarda además `positionsMargin`, `currencyIsolatedEquity`, `frozen`, `orderFrozen` y `availableEquity`. `display` añade `balanceSource`, `marginUsed` y `marginSource`.
- Tests:
  - contrato del panel con la etiqueta, la tarjeta y los tooltips en los 14 estados, y la identidad saldo libre + margen + no realizado = Equity;
  - ejemplos literales de la doc (balance y positions) con sus alternativas de reserva;
  - FakeBlofin descuenta el margen aislado del saldo, como la cuenta demo real.

## 1.7.2

- **"Equity: Sin precio Blofin fresco" / "No disponible" intermitente.** 1.7.0 bajó el límite de frescura del precio a 10 s (ts del libro) pero el vigilante de posiciones seguía refrescando cada 20 s: durante ~la mitad de cada ciclo el panel marcaba las posiciones como sin precio. Ahora el vigilante refresca cada 5 s y el panel solo llama "sin precio" a un precio de más de 30 s (`DISPLAY_MAX_AGE_SECONDS`). La ejecución (SL/TP, entradas, cierres) sigue exigiendo un precio de ≤ 10 s pedido justo antes de actuar.
- Con el bot detenido y posiciones abiertas (en demo viven en Blofin) el precio y el PnL siguen actualizándose para el panel, sin ejecutar SL/TP desde el bot.
- Demo: la cuenta Blofin se lee con el formato de la documentación (`totalEquity`, `details[USDT].equity/balance/available/isolatedUnrealizedPnl`) y se guarda en `exchangeAccount`; la Equity del panel en demo es la de Blofin (sin sumar dos veces el PnL no realizado). Cada posición demo guarda `markPrice` y `unrealizedPnl` de Blofin (`exchangeMark`), que el panel muestra si falta un precio local fresco.

**Auditoría de robustez 1.7.2** (mismo tipo de fallo: dos piezas con supuestos distintos):

- Panel: un fallo puntual de refresco ya no borra un precio que sigue dentro de los 30 s; la frescura de pantalla depende solo de la edad del último precio Blofin bueno.
- Panel: la edad del saldo Blofin se calcula en el servidor (`ageSeconds`), no con el reloj del navegador contra un ts del servidor. El sello `fetchedAtMs` lo pone el servidor.
- Panel: balance, equity, no realizado y PnL salen de un único cálculo del servidor (`display`). En demo la Equity es `totalEquity` de Blofin, nunca saldo + no realizado; la tarjeta Balance muestra `details[USDT].balance`. Marcas Blofin de más de 180 s no se muestran.
- Panel: un error de dibujo ya no se presenta como "Sin conexión con el servidor"; se ve "Error del panel: …" y va a la consola. Celdas del escaneo/historial sin "undefined"/"NaN" con datos incompletos; historial escapado.
- `/api/state` y `/api/health` nunca emiten `NaN`/`Infinity` (JSON inválido que dejaba el panel en "Sin conexión").
- Demo: los pares que no existen en Blofin demo (IOST-USDT) se excluyen del escaneo y de las entradas; la lista se carga al arrancar, antes del primer escaneo, y se conserva si una recarga falla.
- Demo: lectura defensiva de las respuestas de Blofin (`num()`): una posición ilegible nunca cuenta como "sin posición" (estado seguro), fills con tamaño 0 o ts ilegible ya no dan ZeroDivisionError/ValueError, funding no numérico se ignora, un `averagePrice` "0.000" (orden aún sin fill, como en la doc) nunca se apunta como entrada a 0, NaN/inf se descartan.
- Demo: un rechazo del limitador de peticiones antes del POST es una negativa limpia (`not_sent`), no una orden ambigua (antes: estado seguro falso). La consulta tras ambigüedad tolera límites y errores.
- Demo: el SL/TP adjunto que aparece tarde en `orders-tpsl-pending` se reconsulta antes de colocar un segundo TPSL (sin SL duplicado).
- Demo: los cierres que fallan se reintentan cada 15 s (antes cada 5 s), con el control en memoria para que un reinicio reintente de inmediato.
- Reloj: se mide la desviación del reloj local contra la cabecera `Date` de Blofin (mediana de 20); con más de 2 s hay alerta y sale en `/api/health` (`clockSkewSeconds`).
- Pares: el disyuntor solo degrada por fallos seguidos; fallos aislados separados más de 60 s ya no se acumulan.
- Hilos: escaneo, vigilante y trading se supervisan (se reinician si fallan o terminan, con alerta) y laten en cada vuelta y en cada par del escaneo; `/api/health` muestra `threads` y `ok=false` si alguno se para.
- El escaneo registra su duración (`lastScanSeconds`) y avisa si se acerca a la vigencia de las señales.
- Tests nuevos: invariantes de tiempos (fallan con los valores de 1.7.0), contrato panel↔API con render headless real de `index.html` (jsdom) en 14 estados, simulación de ~6 h con reloj falso (latencia, timeouts, 5xx, 429, libros vacíos, IOST, reinicios, paper→demo→paper→demo, bot parado, kill switch), ejemplos literales de la doc de Blofin y variantes malformadas, universo demo, desviación de reloj, disyuntor, supervisión y estrés concurrente.

## 1.7.1

- **El estado sobrevive a los reinicios.** `load_state()` solo conservaba las claves de `default_state()`, así que `killSwitch`, `paperSnapshot`, `balanceSource` y `lastReconcileAt` se perdían en cada reinicio: un kill switch activado por API se desactivaba al reiniciar y la copia del libro paper se volvía a tomar con el saldo demo. Ahora son claves persistentes (más `ledgerMode`, `demoLedger` y `migrations`).
- **Libros paper y demo separados.** En demo el libro activo es el de la cuenta Blofin: historial propio (solo operaciones demo) y saldo inicial = primer saldo que devuelve Blofin. El libro paper se guarda una sola vez al pasar de paper a demo y nunca con un saldo de Blofin. Al volver a `trading_mode: paper` se restaura tal cual; el libro demo queda guardado para la próxima vez. En demo, Iniciar ignora el saldo inicial del formulario y Reset sigue desactivado; un Reset en paper conserva el kill switch y el libro demo.
- **Migración única 1.7.1** para estados de 1.7.0 en demo: si la copia paper falta o tiene el saldo demo, se reconstruye con `initialBalance` + suma del PnL de las operaciones paper (las filas sin `exchange`), p. ej. 10000 + 382,24 = 10382,24 USDT con sus 8 operaciones; el libro demo se queda solo con las operaciones de Blofin. Se ejecuta una vez (`migrations`).
- **Panel**: insignia de modo (PAPER / DEMO / BLOQUEADO), insignia de ESTADO SEGURO con sus motivos y avisos (error de trading, kill switch, alertas de trading). PnL del libro activo; "—" mientras demo espera el primer saldo. En demo se desactivan Reset, saldo inicial, riesgo y apalancamiento (vienen de las opciones del complemento).

## 1.7.0

- **Trading en Blofin Demo (opcional).** `trading_mode: paper` sigue siendo el defecto y no cambia nada. Con `trading_mode: demo` y las tres claves de una API key de *Demo Trading*, las entradas Gran Mago se ejecutan en `https://demo-trading-openapi.blofin.com`: orden a mercado con `clientOrderId` determinista por señal, SL y TP adjuntos a la orden (y, si no aparecen, TP/SL de posición), margen aislado, modo `net`, tamaño redondeado a `lotSize`/`minSize`/`maxMarketSize` y precios a `tickSize` por el lado seguro. Tras un timeout se consulta por `clientOrderId`, nunca se reenvía a ciegas; si el resultado sigue siendo desconocido el bot entra en **estado seguro** y no abre nada más. Reconciliación al arrancar y cada minuto (posiciones, TP/SL vivos, órdenes pendientes). Presupuesto de 25 peticiones privadas por 10 s (Blofin permite 30). Cierres, PnL, comisiones y funding salen de los fills reales de Blofin. Diario fiscal propio `fiscal_journal_demo.sqlite` (modo `demo`, campos del diario de la PR #14).
- **`trading_mode: live` (dinero real) bloqueado en código**: el bot no arranca el trading y muestra el error.
- **Kill switch**: opción `kill_switch` y `POST /api/kill_switch {"enabled": true, "closeAll": true|false}`. Bloquea entradas nuevas y, si se pide, cierra todas las posiciones (en demo, a mercado en Blofin).
- **Seguridad (C5)**: los endpoints que cambian algo (`start`, `stop`, `reset`, `settings`, `scan`, `close_position`, `kill_switch`) solo responden desde el panel de HA (ingress), loopback, o con la cabecera `X-GranMago-Token` igual a la opción `control_token`. Las lecturas (`/api/state`, `/api/health`) siguen abiertas. Las claves son opciones `password`, se pasan por entorno, se censuran en logs y nunca salen en el estado.
- **Robustez de datos Blofin (informe P0–P4)**: precio de ejecución = mid de `books?size=1` y frescura por el `ts` del libro (≤ 10 s), en vez del `last`/`ts` del ticker, que envejece en pares sin trades (C1). La vela recién cerrada se acepta 180 s tras el cierre de 4H (Blofin publica la nueva a los ~70 s, C2). Los pares fallidos se reintentan al final del escaneo. Motivo exacto de cada rechazo y contadores por endpoint (ok, fallos por motivo, p50/p95) en `/api/state` → `market` y `/api/health`. Sesión HTTP compartida, instrumentos en caché 10 min, timeouts (3 s, 3 s) y backoff con jitter. Un par con 3 fallos seguidos o 60 s sin precio válido queda **degradado** (sin entradas) y necesita un ciclo sano antes de volver a abrir.
- Nuevo `GET /api/health` (versión, modo, estado seguro, kill switch, pares degradados, alertas) para sensores REST de HA.

## 1.6.2

- Fuente de mercado exclusiva Blofin para velas y ticker fresco de entradas, PnL y todos los cierres; se elimina el fallback externo y el uso de cierres de vela como fills en vivo.
- Validación de instrumento, timestamps/frescura, valores finitos, libro y coherencia de entrada. Sin precio válido no se ejecuta; la interfaz no presenta PnL/equity obsoletos como actuales.
- Reintentos GET Blofin acotados con backoff, presupuesto, limitación global de tasa y Retry-After. Monitor por posición en paralelo, sin red bajo lock ni duplicación de operaciones.
- Auditoría de proveedor/timestamp/precio para nuevos fills. No reescribe historial, no ajusta la pérdida XMR ni cambia estrategia o fórmula de riesgo. Tests aislados de regresión, errores, frescura, reintentos y concurrencia; replays históricos separados de las puertas en vivo.

## 1.6.1

- No cambia el comportamiento respecto a 1.5.2. El complemento ya estaba instalado como 1.6.0 por una etiqueta anterior, puesta sin implementar la estrategia v7, y Home Assistant no ofrece una versión más baja. Esta sube a 1.6.1 para que la actualización sí aparezca.
- Trae las correcciones de 1.5.2: una señal (par, dirección y cierre de la vela de 4H) abre como máximo una operación; no se entra si el precio del escaneo ya está en el stop o más allá, o ya ha llegado al objetivo, y la entrada usa ese precio; `pairs: []` vuelve a los 28 pares y sustituye la lista guardada en el estado. La estrategia v7 sigue sin estar implementada.

## 1.5.2

- Una señal (par, dirección y cierre de la vela de 4H) abre como máximo una operación. El escaneo de cada 90 s seguía viendo la misma señal hasta el siguiente cierre, y al saltar el stop volvía a entrar al precio de esa vela aunque el mercado ya estuviera por debajo. La entrada usa el precio del escaneo. Si ese precio ya está en el stop o más allá, o ya ha llegado al objetivo, la operación no se abre.
- `pairs: []` usa los 28 pares de la monitorización y sustituye la lista guardada en el estado (por ejemplo los 12 de 1.5.0). bashio exporta una lista vacía como cadena vacía; antes eso se leía como opción ausente y ganaba el `state.json`.

## 1.5.1

- El complemento arranca. 1.5.0 copiaba una lista fija de archivos y no metía `granmago_strategy.py` en la imagen, así que el proceso moría con `ModuleNotFoundError` antes de escanear. El Dockerfile copia ese módulo, y un test falla si `server.py` importa un módulo local que la imagen no incluye.
- La página de configuración se queda en lo que un usuario cambia de verdad: `log_level`, `initial_balance`, `risk_pct` (1), `leverage` (3), `max_positions` (3), `fee_pct_per_side` (0,06), `slippage_pct_per_side` (0,02) y `pairs` (los 28 del walk-forward). El resto de la monitorización queda fijo: estrategia Gran Mago, variante A, estilo sweep, setups both, sin promedios, salida por agotamiento, 15 velas, margen de stop 0,3 %, objetivo 1,7 R, trailing apagado, sin filtro de régimen, sin enfriamiento y escaneo cada 90 s.
- Una instalación que venga de 1.5.0 trae las claves viejas (`strategy`, `entry_threshold`, `trailing_*`, `regime_*`, `granmago_*` y el resto). El esquema ya no las declara. El arranque las ignora y, si la API del Supervisor responde, las borra para que no se queden avisos. Si esa API no responde, el proceso arranca igual. El Supervisor solo rellena el defecto de una clave que falta: riesgo, apalancamiento, máximo de posiciones, comisiones y pares que ya estuvieran guardados se conservan. Una instalación nueva usa los defectos de arriba.
- El estado de `/data` (balance, posiciones e historial), incluido un `openPosition` de versiones anteriores o un libro de 1.5.0, se carga sin cerrar operaciones.

## 1.5.0

- Modo de estrategia seleccionable. `strategy: scanner` (el defecto) deja el bot de 1.4.0 igual. `strategy: granmago` entra y sale con las reglas del curso de Gran Mago, no con el score.
- Variante `a`: largos y cortos, confirmación con dos cierres de 4H, sin promediar. Variante `b`: solo largos, confirmación con un cierre de 12H (las velas de 12H salen de tres velas de 4H de Blofin, 00:00 y 12:00 UTC).
- Tendencia por la EMA50 semanal y diaria. Si se contradicen, manda la diaria. Hacen falta dos cierres seguidos al otro lado de la media para cambiar de fase.
- Setup A: barrido del pivote de cierre diario más extremo aún no barrido (5 velas a cada lado, ventana de 30 días) y los cierres de confirmación al otro lado del nivel con más toques (±0,2 %). Una ventana de 90 días dejaba el origen de la tendencia sin barrer y bloqueaba el setup. Setup B: retroceso a favor de tendencia hacia la EMA50 diaria, un máximo/mínimo roto, un imbalance de tres velas o el 50 % de una mecha grande. No se persigue una vela posterior.
- Estocástico 14, 3, 3 como veto (no hay largo con %K > 80 ni corto con %K < 20). El squeeze es el de LazyBear (BB 20/2, KC 20/1,5 sobre la media del true range) y el ADX(14) tiene que estar subiendo. El detalle está en el README.
- El stop queda más allá del extremo barrido, con margen `max(0,5 × ATR14 de 4H, granmago_stop_pad_pct)`. El objetivo es el clúster del swing diario anterior, un 0,1 % por dentro. `granmago_min_rr` en 0 no exige una relación mínima, porque en sus cifras el beneficio medio es menor que la pérdida media.
- Salida opcional por agotamiento del estocástico y del squeeze (`granmago_momentum_exit`, activa) y por tiempo a las 15 velas de confirmación. Sin trailing en este modo. El breakeven (`granmago_breakeven_r`) sale en 0.
- Promedios opcionales (`granmago_max_adds`, 0, 1 o 2) solo al tocar el extremo barrido y antes del stop. La primera entrada reserva el riesgo de los añadidos. Un cierre de confirmación al otro lado del extremo no añade más.
- El riesgo por defecto sigue en 2 % y el apalancamiento en 3, que es lo que él publica para esta estrategia. No hay filtro de noticias: el curso y X se contradicen y aquí no hay calendario.
- `granmago_style: sweep` relaja el setup A para que el barrido sea la entrada. El nivel puede ser un pivote diario de los últimos 20 días, un pivote de 4H de los últimos 30 bars o el máximo/mínimo del día anterior; si se barren varios, se usa el más extremo. El cierre tiene que volver al otro lado de ese precio en las velas de confirmación, y si una vela no lo recupera el barrido se cancela. La tendencia es solo el último cierre diario contra la EMA50. El estocástico confirma con un cruce fuera de 20 (largo) u 80 (corto) en vez de vetar. El squeeze y el ADX se anotan y no bloquean. `granmago_setups` elige `both`, `sweep` o `pullback`. El defecto sigue siendo `strict` y `both`.
- `pairs` elige qué swaps USDT se escanean. El defecto sigue siendo BTC, ETH, SOL, XRP, BNB, ADA, DOGE, AVAX, LINK, DOT, LTC y BCH. El README lista los 28 pares líquidos del walk-forward (esos 12 más ZEC, PEPE, 1000BONK, NEAR, XMR, INJ, SUI, TAO, IOST, XLM, TRX, HBAR, ENA, WLD, AAVE y QNT). Una lista vacía o inválida vuelve a los 12. Como máximo 40 símbolos. Una posición ya abierta fuera de la lista se sigue vigilando.

## 1.4.0

- Comisión taker y slippage en el paper y en el backtest (`fee_pct_per_side`, `slippage_pct_per_side`). Por defecto 0, así que el balance de una instalación actual no cambia hasta que se pongan. El taker de Blofin ronda 0,06 % y un slippage de 0,02 % por lado es el supuesto usado en la evaluación.
- El trailing puede armarse en múltiplos de R (`trailing_activation_r`). Con 0 se mantiene la activación en porcentaje del nocional. `trailing_keep_tp` deja el objetivo puesto al armarse; por defecto se sigue quitando.
- Objetivo configurable (`take_profit_r`, 1,7) y parcial opcional (`partial_tp_r` en 0, `partial_tp_fraction` 0,5). Tras el parcial el stop puede pasar a breakeven.
- Stop por ATR(4H) opcional (`stop_mode`: `swing` o `atr`). El defecto sigue siendo el swing de 10 velas, con el mismo suelo del 1,2 % y el mismo tope del 8 %.
- Enfriamiento por par (`pair_cooldown_minutes`, 0) y tope de riesgo abierto en la misma dirección (`max_same_direction_risk_r`, 0). El tope cuenta el riesgo inicial de cada posición hasta que se cierra del todo.
- El historial guarda `initialSl`, `riskAmt`, `initialRiskAmt`, `rMultiple` y, si hubo parcial, `tradePnl` y `tradeR` de la operación completa.
- El score ya no recorta 105 a 100. El sesgo pasa de 35 a 30 puntos para que el bonus de estocástico quepa en 100. 100 exige el bonus; 95 es el setup completo sin él. 90 sigue coincidiendo con 95, porque el siguiente score real es 80.
- Filtro de régimen opcional (`regime_filter`: `off`, `slope`, `adx`, `er`, `slope_adx`, `slope_er`). `off` no cambia las entradas. `slope` usa la pendiente de la EMA50 diaria (5 velas): no abre largos si baja ni cortos si sube. ADX(14) y el efficiency ratio de Kaufman (20 velas de 4H) exigen un mínimo (`regime_adx_min` 20, `regime_er_min` 0,2).
- `risk_pct` y `leverage` del complemento se aplican en cada arranque y pisan los valores guardados en `state.json`. La página puede cambiarlos hasta el siguiente reinicio.

## 1.3.0

- Hasta `max_positions` operaciones abiertas a la vez (entero, por defecto 5, de 1 a 12). Cada una va en una moneda distinta: no se repite el par ni se cubre el mismo activo en largo y corto.
- En cada escaneo automático se rellenan los huecos libres con los pares que llegan a `entry_threshold`, el mejor score primero. Varios pares del mismo escaneo pueden abrirse. Un escaneo manual no abre operaciones.
- Cada posición lleva su propio SL, TP y trailing (activación sobre su nocional, distancia sobre su riesgo inicial, breakeven, solo se aprieta). Los motivos de cierre siguen siendo SL, TP, trailing y manual.
- El riesgo de cada entrada sigue siendo `risk_pct` del balance realizado en ese momento. Con 5 posiciones el riesgo a stop puede llegar a 5 × `risk_pct`. El 2% por defecto no cambia.
- El nocional nuevo se recorta, o no se abre, si la suma de nocionales pasaría de balance × apalancamiento. Una posición ya abierta no se recorta ni se cierra por ese tope.
- La página muestra la tabla de posiciones, el PnL no realizado, el equity y «x/5 posiciones». `POST /api/close_position` recibe `{"pair": "ETH-USDT"}`.
- Un `state.json` de 1.2.0 con `openPosition` (la short de ETH-USDT incluida) pasa a la lista al arrancar, sin cerrarla. Balance e historial se conservan.

## 1.2.0

- La entrada automática exige el score máximo: opción `entry_threshold` (entero, por defecto 100, de 0 a 100). Con el valor por defecto solo abre si el score es 100.
- El ranking marca LONG/SHORT solo cuando el score llega a ese umbral. El escaneo, una sola posición, el cierre manual y el trailing no cambian.

## 1.1.0

- El escaneo automático sigue cada intervalo aunque haya una posición abierta. Actualiza las señales y no abre una segunda posición.
- Botón **Cerrar posición** y `POST /api/close_position`: cierra a mercado, motivo `manual`, mismo cálculo de balance que un SL/TP.
- Trailing stop (activo por defecto). Se arma cuando el PnL no realizado llega a `trailing_activation_pct` (1% del nocional). Entonces desaparecen el TP y el SL iniciales y solo puede cerrar el trailing, con motivo `trailing`, o un cierre manual. La distancia es `trailing_distance` (0.5 del riesgo inicial) desde el mejor precio. Al activarse el stop queda como mínimo en el breakeven y después solo se aprieta.
- `trailing_enabled` en false deja el SL y el TP fijos.
- No hay cierre por tiempo. Un estado de 1.0.0 con posición abierta se carga, se rellenan los campos que falten y la operación sigue.

## 1.0.0

- Versión inicial del complemento.
