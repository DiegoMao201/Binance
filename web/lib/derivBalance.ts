/**
 * lib/derivBalance.ts — Lectura server-side del balance Deriv.
 *
 * SOLO se ejecuta server-side (Next.js API route / Server Component).
 * El token Deriv del cliente NUNCA debe llegar al browser.
 *
 * Flujo (alineado al bot Python):
 *   1) POST OTP: https://api.derivws.com/trading/v1/options/accounts/{account_id}/otp
 *      Headers: Deriv-App-ID + Authorization Bearer <PAT>
 *   2) Recibe data.url (WS preautenticado)
 *   3) WS one-shot -> { balance: 1 } -> close
 *
 * Diseno:
 *  - Una conexion por llamada, vida corta.
 *  - Sin authorize legacy (el WS OTP ya viene autenticado).
 *  - Sin "subscribe" persistente: el frontend hace polling cada 5s.
 *  - Timeout estricto para no bloquear el route handler.
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

const DERIV_OTP_URL = "https://api.derivws.com/trading/v1/options/accounts";
const DEFAULT_APP_ID = (process.env.DERIV_APP_ID ?? "1089").trim();
const TIMEOUT_MS = 7_000;

interface DerivMessage {
  msg_type?: string;
  error?: { code?: string; message?: string };
  balance?: { balance?: number | string; currency?: string; loginid?: string };
}

/**
 * Lee el balance actual de la cuenta Deriv asociada al token dado.
 * Nunca lanza; siempre devuelve un snapshot { ok, error? }.
 */
export async function fetchDerivBalance(
  token: string,
  accountId: string | null | undefined,
): Promise<DerivBalanceSnapshot> {
  const fetchedAt = new Date().toISOString();

  if (!token || typeof token !== "string" || token.length < 8) {
    return { ok: false, fetchedAt, error: "Token Deriv invalido o ausente." };
  }
  if (!accountId || typeof accountId !== "string" || accountId.trim().length < 3) {
    return { ok: false, fetchedAt, error: "Cuenta Deriv invalida o ausente." };
  }

  const otpUrl = `${DERIV_OTP_URL}/${encodeURIComponent(accountId.trim())}/otp`;

  let wsUrl: string;
  try {
    const otpResp = await fetch(otpUrl, {
      method: "POST",
      headers: {
        "Deriv-App-ID": DEFAULT_APP_ID,
        Authorization: `Bearer ${token.trim()}`,
        "Content-Type": "application/json",
      },
      cache: "no-store",
    });

    const raw = await otpResp.text();
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw) as unknown;
    } catch {
      parsed = null;
    }

    if (!otpResp.ok) {
      const msg = (parsed as { error?: { message?: string } } | null)?.error?.message
        ?? raw.slice(0, 180)
        ?? "OTP rechazado";
      return {
        ok: false,
        fetchedAt,
        error: `OTP Deriv fallo (${otpResp.status}): ${msg}`,
      };
    }

    wsUrl = String((parsed as { data?: { url?: string } } | null)?.data?.url ?? "").trim();
    if (!wsUrl) {
      return {
        ok: false,
        fetchedAt,
        error: "OTP Deriv no retorno data.url.",
      };
    }
  } catch (err) {
    return {
      ok: false,
      fetchedAt,
      error: `Error solicitando OTP Deriv: ${(err as Error).message}`,
    };
  }

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
      ws = new WebSocket(wsUrl);
    } catch (err) {
      clearTimeout(timer);
      finish({
        ok: false,
        fetchedAt,
        error: `No se pudo abrir WS OTP Deriv: ${(err as Error).message}`,
      });
      return;
    }

    ws.addEventListener("open", () => {
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

      if (msg.msg_type === "balance" && msg.balance) {
        const numericBalance =
          typeof msg.balance.balance === "number"
            ? msg.balance.balance
            : Number(msg.balance.balance ?? 0);
        clearTimeout(timer);
        finish({
          ok: true,
          fetchedAt,
          balance: Number.isFinite(numericBalance) ? numericBalance : 0,
          currency: msg.balance.currency,
          loginid: msg.balance.loginid,
        });
      }
    });

    ws.addEventListener("error", () => {
      clearTimeout(timer);
      finish({ ok: false, fetchedAt, error: "WebSocket error contra Deriv (OTP)." });
    });

    ws.addEventListener("close", () => {
      if (!settled) {
        clearTimeout(timer);
        finish({ ok: false, fetchedAt, error: "WS OTP Deriv cerrado antes de balance." });
      }
    });
  });
}
