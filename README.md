# GranMago Scanner

Complemento de Home Assistant. Paper trading del scanner GranMago (EMA50 semanal / diario / 4H, estocástico y zona de liquidez).

El bucle corre **dentro del servidor**. Puedes cerrar la página o apagar la pantalla del móvil: la posición, el balance y el historial siguen en el addon.

No envía órdenes a ningún exchange. No es asesoramiento financiero.

## Instalar en Home Assistant

1. Ajustes → Complementos → Tienda de complementos → ⋮ → **Repositorios**.
2. Añade `https://github.com/iejayu8/granmago-scanner` y espera a que recargue.
3. Instala **GranMago Scanner**, arráncalo y activa "Iniciar con Home Assistant" si quieres que vuelva solo tras un reinicio.
4. Abre el panel **GranMago** en la barra lateral (o `http://<tu-home-assistant>:8787`).
5. Pulsa **Iniciar bot** una vez. A partir de ahí sigue solo.

El puerto 8787 no choca con HADomotics (8099).

## Qué hace

- Vigila una posición abierta cada 20 segundos con el precio en vivo.
- Si no hay posición, escanea BTC, ETH, SOL, XRP, BNB, ADA, DOGE, AVAX, LINK, DOT, LTC y BCH cada 90 segundos (configurable).
- Entra en paper solo si el score llega a 75 y el sesgo semanal acompaña.
- Guarda el estado en `/data/state.json` del addon. Un reinicio del complemento no borra el balance ni la operación abierta, y si estaba en marcha se reanuda solo.

## Ajustes del complemento

| Opción | Por defecto | Uso |
|---|---|---|
| risk_pct | 2 | % del balance arriesgado por operación |
| leverage | 3 | Se guarda en la operación (el tamaño sale del riesgo y del stop) |
| initial_balance | 10000 | Balance inicial de paper, solo al resetear |
| scan_seconds | 90 | Segundos entre escaneos completos |
| log_level | info | Log del complemento |
