# GranMago Scanner (v7 Pullback)

Complemento de Home Assistant. Paper trading con la estrategia **v7 Pullback-to-EMA + RSI Recovery** (15m).

El bucle corre **dentro del servidor**. Puedes cerrar la página o apagar la pantalla del móvil: la posición, el balance y el historial siguen en el addon.

No envía órdenes a ningún exchange. No es asesoramiento financiero.

## Instalar en Home Assistant

1. Ajustes → Complementos → Tienda de complementos → ⋮ → **Repositorios**.
2. Añade `https://github.com/iejayu8/granmago-scanner` y espera a que recargue.
3. Instala **GranMago Scanner**, arráncalo y activa "Iniciar con Home Assistant" si quieres que vuelva solo tras un reinicio.
4. Abre el panel **GranMago** en la barra lateral (o `http://<tu-home-assistant>:8787`).
5. Pulsa **Iniciar bot** una vez. A partir de ahí sigue solo.

## Estrategia v7 (sustituye la anterior)

- Timeframe: **15 minutos**
- Pares: BTC, ETH, SOL, XRP, LINK
- Entrada LONG: precio > EMA200, EMA21 > EMA55, RSI cayó <46 en últimas 3 velas, RSI actual ≥49, precio > EMA9, MACD hist > -50, volumen > 0.9×SMA20
- SHORT: condiciones espejo
- SL 2.5% / TP 4.0% (R:R 1.6:1)
- Riesgo configurable (por defecto 2%)

## Ajustes del complemento

| Opción | Por defecto | Uso |
|---|---|---|
| risk_pct | 2 | % del balance arriesgado por operación |
| leverage | 3 | Se guarda en la operación |
| initial_balance | 10000 | Balance inicial de paper |
| scan_seconds | 90 | Segundos entre escaneos |
| log_level | info | Log del complemento |