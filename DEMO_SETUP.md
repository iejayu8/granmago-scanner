# GranMago 1.7.0 — operar en Blofin Demo Trading

`paper` sigue siendo el modo por defecto. El modo `demo` manda órdenes a la cuenta **Demo Trading** de Blofin (`https://demo-trading-openapi.blofin.com`, dinero ficticio). El modo `live` (dinero real) está **bloqueado en el código**: si se elige, el bot no opera y muestra `trading_mode=live (dinero real) está BLOQUEADO`.

## 1. Crear la API key de Demo Trading

1. En blofin.com, entra en tu cuenta y cambia a **Demo Trading** (selector de cuenta demo).
2. Si la cuenta demo no tiene USDT, pide fondos demo desde la propia web.
3. Abre la página **API** (gestión de API keys) *estando en Demo Trading* y crea una key nueva:
   - Tipo: API normal (no "BloFin MCP").
   - Permisos: **READ + TRADE**. **No** marques TRANSFER ni ninguna retirada. El bot rechaza una key de solo lectura y avisa si no está restringida por IP.
   - **IP**: añade la IP pública de salida de tu Home Assistant (la de tu router, si es fija). Blofin admite hasta 20 IPs. Una key sin IP caduca a los 90 días, así que también vale para la prueba de una semana, pero con IP es más seguro. Si tu IP cambia (IP dinámica), Blofin rechazará las peticiones con un error de IP y el bot entrará en estado seguro: lo verás en `/api/health`.
   - Elige una **passphrase** y apúntala.
4. Guarda los tres valores: **API Key**, **Secret Key** y **Passphrase**. El secret solo se muestra una vez.

Una key de la cuenta real **no** funciona en el endpoint demo (y el bot no tiene endpoint real habilitado).

## 2. Dónde van las opciones

Home Assistant → Ajustes → Complementos → GranMago Scanner → **Configuración**:

| Opción | Valor |
|---|---|
| `trading_mode` | `demo` |
| `blofin_api_key` | API Key de demo |
| `blofin_api_secret` | Secret Key de demo |
| `blofin_api_passphrase` | Passphrase de demo |
| `control_token` | opcional: una cadena larga aleatoria si quieres controlar el bot desde fuera del panel (rest_command de HA, scripts) |
| `kill_switch` | `false` (ponlo a `true` para impedir entradas nuevas desde el arranque) |

Las tres claves y el `control_token` son campos de contraseña: HA no los muestra, el complemento los recibe por variables de entorno, los censura en los logs (`***`) y no salen nunca en `/api/state` ni en `/api/health`.

`risk_pct`, `leverage` y `max_positions` siguen aplicando. `leverage` se fija en Blofin por instrumento, en margen **aislado**.

## 3. Activar demo

1. Pon las opciones de arriba y pulsa **Guardar**.
2. **Reinicia** el complemento. En el log debe aparecer `Modo DEMO Blofin (https://demo-trading-openapi.blofin.com)`.
3. Al arrancar, el bot: consulta la key (`readOnly`, IPs), comprueba el desfase de reloj (≤ 5 s), pone la cuenta en modo de posición **net** (solo si no hay posiciones abiertas) y margen **isolated**, revisa intenciones de orden pendientes y reconcilia con Blofin. Si algo falla, queda en **estado seguro** (no abre nada) y lo dice en `/api/health`.
4. Abre el panel y pulsa **Iniciar** como siempre.

Volver a paper: `trading_mode: paper` y reiniciar. Las posiciones demo que sigan abiertas en Blofin **no** se cierran solas al cambiar de modo; ciérralas antes con el kill switch (`closeAll`) o desde la web de Blofin.

En demo, **Reset** y el cambio de ajustes desde el panel están desactivados (el balance sale de la cuenta Blofin). El cierre manual desde el panel se encola y se ejecuta a mercado en Blofin (respuesta 202).

## 4. Cómo se protege cada operación

- Una señal = un `clientOrderId` determinista. La señal se marca como usada y se escribe una intención (`/data/trading_intents.jsonl`, con fsync) **antes** de enviar la orden. Un reinicio nunca repite la señal.
- Orden a mercado con **SL y TP adjuntos** (precio de disparo `last`, ejecución a mercado). Tras el fill se verifica que el TP/SL existe; si no, se coloca un TP/SL de posición; si tampoco se puede, **se cierra la posición**.
- Timeout o error 5xx al enviar: se consulta la orden por `clientOrderId`; **no se reenvía**. Si no se puede saber qué pasó, el bot entra en estado seguro.
- Reconciliación cada minuto: posición que solo existe en Blofin, tamaño distinto u órdenes pendientes → alerta y estado seguro (sin entradas nuevas). Posición local que ya no está en Blofin → se liquida con los fills reales (precio, comisiones, PnL, funding). Posición sin SL → se recoloca.
- El breakeven se aplica en Blofin poniendo el SL nuevo **antes** de cancelar el viejo.
- Máximo 25 peticiones privadas por 10 s (Blofin permite 30).
- Cada cierre se anota en `/data/fiscal_journal_demo.sqlite` (modo `demo`) con IDs reales de orden y de trade, comisiones y funding.

Salir del estado seguro: revisa la causa en `/api/health` → `unsafeReasons`, arréglala (en la web de Blofin si hace falta) y reinicia el complemento.

## 5. Kill switch

- Opción `kill_switch: true` + reinicio: sin entradas nuevas.
- En caliente: `POST /api/kill_switch` con `{"enabled": true}` (solo bloquea entradas) o `{"enabled": true, "closeAll": true}` (además cierra todas las posiciones a mercado en Blofin). `{"enabled": false}` lo desactiva.
- Las posiciones abiertas mantienen su SL/TP en Blofin aunque pares el bot o lo apagues.
- Desde el panel de HA funciona sin token. Desde fuera (otra máquina, `rest_command` de HA) hace falta `control_token` y la cabecera `X-GranMago-Token`.

Ejemplo de `configuration.yaml` de HA (solo documentación, no se ha aplicado nada):

```yaml
rest_command:
  granmago_kill_all:
    url: "http://<IP_HA>:8787/api/kill_switch"
    method: POST
    headers:
      X-GranMago-Token: !secret granmago_control_token
    content_type: "application/json"
    payload: '{"enabled": true, "closeAll": true}'

rest:
  - resource: "http://<IP_HA>:8787/api/health"
    scan_interval: 60
    sensor:
      - name: "GranMago salud"
        value_template: "{{ 'ok' if value_json.ok else 'ALERTA' }}"
        json_attributes: [mode, safe, unsafeReasons, killSwitch, openPositions, degradedPairs, alerts]
```

## 6. Qué revisar el primer día

1. Log de arranque: `Modo DEMO Blofin`, sin `TRADING BLOQUEADO` ni `estado seguro`.
2. `GET /api/health`: `mode: demo`, `ok: true`, `safe: true`, `unsafeReasons: []`. En `/api/state` → `trading`: `accountReady: true`, `apiKey.readOnly: 0`, `apiKey.ipsBound: true`, `serverSkewSeconds` pequeño.
3. Primera entrada: en la web de Blofin Demo deben verse la posición (aislada, apalancamiento esperado) **y** su TP/SL. Compara contratos, precio medio y SL con el panel.
4. `/api/state` → `market`: `failRate` bajo en `books`, `instruments` y `candles:4H`; `degraded` vacío o con pares que se recuperan.
5. Al menos un cierre: aparece en el historial con `exitOrderId`, `exchangeTradeIds` y `fundingUsdt`, y en `fiscal_journal_demo.sqlite`.
6. Prueba el kill switch una vez sin `closeAll` y desactívalo.
7. `lastReconcileAt` en el estado avanza cada minuto.
