/**
 * lib/derivBalance.ts — Lectura server-side del balance Deriv.
 *
 * SOLO se ejecuta server-side (Next.js API route / Server Component).
 * El token Deriv del cliente NUNCA debe llegar al browser; este modulo
 * lo recibe como argumento explicito y abre una sesion WebSocket
 * efimera contra wss://ws.binaryws.com/websockets/v3.
 *
 * Diseno:
 *  - Una conexion por llamada, vida corta (<3s).
 *  - Authorize -> balance one-shot -> close.
 *  - Sin "subscribe" persistente: el frontend hace polling cada 5s.
 *  - Timeout estricto de 5s para no bloquear el route handler.
 *
 * Node 22 incluye `WebSocket` global; no requiere `ws` como dependencia.
 */

export interface DerivBalanceSnapshot {
  ok: boolean;
  balance?: number;
  currency?: string;
  loginid?: string;
  fetchedAt: string;
  error?: string;
}

const DERIV_WS_URL = "wss://ws.binaryws.com/websockets/v3";
const DEFAULT_APP_ID = process.env.DERIV_APP_ID ?? "1089"; // public dev app
const TIMEOUT_MS = 5_000;

interface DerivMessage {
  msg_type?: string;
  error?: { code?: string; message?: string };
  authorize?: { loginid?: string };
  balance?: { balance?: number; currency?: string; loginid?: string };
}

/**
 * Lee el balance actual de la cuenta Deriv asociada al token dado.
 * Nunca lanza; siempre devuelve un snapshot { ok, error? }.
 */
export async function fetchDerivBalance(token: string): Promise<DerivBalanceSnapshot> {
  const fetchedAt = new Date().toISOString();

  if (!token || typeof token !== "string" || token.length < 8) {
    return { ok: false, fetchedAt, error: "Token Deriv invalido o ausente." };
  }

  const url = `${DERIV_WS_URL}?app_id=${encodeURIComponent(DEFAULT_APP_ID)}`;

  return new Promise<DerivBalanceSnapshot>((resolve) => {
    let settled = false;
    let ws: WebSocket | null = null;

    const finish = (snap: DerivBalanceSnapshot) => {
      if (settled) return;
      settled = true;
      try { ws?.close(); } catch { /* noop */ }
      resolve(snap);
    };

    const timer = setTimeout(() => {
      finish({ ok: false, fetchedAt, error: `Timeout > ${TIMEOUT_MS}ms.` });
    }, TIMEOUT_MS);

    try {
      ws = new WebSocket(url);
    } catch (err) {
      clearTimeout(timer);
      finish({
        ok: false,
        fetchedAt,
        error: `No se pudo abrir WS Deriv: ${(err as Error).message}`,
      });
      return;
    }

    ws.addEventListener("open", () => {
      try {
        ws?.send(JSON.stringify({ authorize: token }));
      } catch (err) {
        clearTimeout(timer);
        finish({
          ok: false,
          fetchedAt,
          error: `Fallo enviando authorize: ${(err as Error).message}`,
        });
      }
    });

    ws.addEventListener("message", (event: MessageEvent) => {
      let msg: DerivMessage;
      try {
        msg = JSON.parse(String(event.data)) as DerivMessage;
      } catch {
        return;
      }

      if (msg.error) {
        clearTimeout(timer);
        finish({
          ok: false,
          fetchedAt,
          error: `Deriv error [${msg.error.code ?? "?"}]: ${msg.error.message ?? "sin mensaje"}`,
        });
        return;
      }

      if (msg.msg_type === "authorize") {
        try {
          ws?.send(JSON.stringify({ balance: 1 }));
        } catch (err) {
          clearTimeout(timer);
          finish({
            ok: false,
            fetchedAt,
            error: `Fallo solicitando balance: ${(err as Error).message}`,
          });
        }
        return;
      }

      if (msg.msg_type === "balance" && msg.balance) {
        clearTimeout(timer);
        finish({
          ok: true,
          fetchedAt,
          balance: typeof msg.balance.balance === "number"
            ? msg.balance.balance
            : Number(msg.balance.balance ?? 0),
          currency: msg.balance.currency,
          loginid: msg.balance.loginid,
        });
      }
    });

    ws.addEventListener("error", () => {
      clearTimeout(timer);
      finish({ ok: false, fetchedAt, error: "WebSocket error contra Deriv." });
    });

    ws.addEventListener("close", () => {
      if (!settled) {
        clearTimeout(timer);
        finish({ ok: false, fetchedAt, error: "WS Deriv cerrado antes de balance." });
      }
    });
  });
}
