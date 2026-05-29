import sgMail, { type MailDataRequired } from "@sendgrid/mail";

// Initialise once at module load. The key is validated at runtime by SendGrid.
sgMail.setApiKey(process.env.SENDGRID_API_KEY ?? "");

// MAIL_FROM_EMAIL / MAIL_FROM_NAME match the env vars already in Coolify.
const FROM_EMAIL = process.env.MAIL_FROM_EMAIL ?? process.env.SENDGRID_FROM_EMAIL ?? "";
const FROM_NAME  = process.env.MAIL_FROM_NAME  ?? "OptiFerre Portal";
const PRODUCT_NAME = FROM_NAME;

/**
 * Sends a one-time password email to the investor.
 *
 * Errors are NOT swallowed — the caller (API route) decides whether to surface
 * them to the client. This keeps the contract explicit and testable.
 *
 * @throws {Error} if SendGrid returns a non-2xx status or the env vars are absent.
 */
export async function sendOtpEmail(to: string, otp: string): Promise<void> {
  if (!process.env.SENDGRID_API_KEY) {
    console.warn(
      `[email] SENDGRID_API_KEY not set. OTP for ${to}: ${otp}  (expires in 10 min)`
    );
    return;
  }
  if (!FROM_EMAIL) {
    console.warn(
      `[email] MAIL_FROM_EMAIL not set. OTP for ${to}: ${otp}  (expires in 10 min)`
    );
    return;
  }

  const msg: MailDataRequired = {
    to,
    from: { email: FROM_EMAIL, name: FROM_NAME },
    subject: `${PRODUCT_NAME} — Tu código de acceso`,
    text: [
      `Tu código de acceso único para ${PRODUCT_NAME} es:`,
      ``,
      `    ${otp}`,
      ``,
      `Este código es válido durante 10 minutos y solo puede usarse una vez.`,
      `Si no solicitaste este código, ignora este mensaje — tu cuenta sigue segura.`,
    ].join("\n"),
    html: `
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>${PRODUCT_NAME}</title>
</head>
<body style="margin:0;padding:0;background:#0f1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#0f1117;padding:40px 0;">
    <tr>
      <td align="center">
        <table width="480" cellpadding="0" cellspacing="0" style="background:#1a1d2e;border-radius:12px;overflow:hidden;">
          <!-- Header -->
          <tr>
            <td style="background:#6366f1;padding:24px 32px;">
              <p style="margin:0;color:#fff;font-size:20px;font-weight:700;letter-spacing:-0.3px;">
                ${PRODUCT_NAME}
              </p>
            </td>
          </tr>
          <!-- Body -->
          <tr>
            <td style="padding:32px;">
              <p style="margin:0 0 8px;color:#94a3b8;font-size:14px;">Tu código de acceso</p>
              <p style="margin:0 0 24px;color:#f1f5f9;font-size:14px;line-height:1.6;">
                Introduce el siguiente código en el portal para acceder a tu cuenta.
              </p>
              <!-- OTP badge -->
              <div style="background:#0f1117;border:1px solid #334155;border-radius:8px;padding:20px;text-align:center;margin-bottom:24px;">
                <span style="font-size:36px;font-weight:800;letter-spacing:10px;color:#6366f1;font-variant-numeric:tabular-nums;">
                  ${otp}
                </span>
              </div>
              <p style="margin:0 0 8px;color:#64748b;font-size:12px;">
                ⏱ Válido durante <strong style="color:#94a3b8;">10 minutos</strong>.
              </p>
              <p style="margin:0;color:#64748b;font-size:12px;">
                Si no solicitaste este código, puedes ignorar este mensaje con seguridad.
              </p>
            </td>
          </tr>
          <!-- Footer -->
          <tr>
            <td style="padding:16px 32px;border-top:1px solid #1e293b;">
              <p style="margin:0;color:#334155;font-size:11px;text-align:center;">
                © ${new Date().getFullYear()} ${PRODUCT_NAME} — Portal de Inversores Institucional
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>`,
    // Disable tracking to reduce phishing-detection false positives.
    trackingSettings: {
      clickTracking: { enable: false },
      openTracking: { enable: false },
    },
  };

  await sgMail.send(msg);
}

// ─── sendTradeClosedEmail ──────────────────────────────────────────────────────

/**
 * Sends a "trade closed — WIN" notification to an investor.
 *
 * Errors are NOT swallowed — the caller (webhook route) wraps each send in
 * an individual try/catch so one failure does not block the rest.
 *
 * @throws {Error} if SendGrid returns a non-2xx status or env vars are absent.
 */
export async function sendTradeClosedEmail(
  to: string,
  investorName: string | null,
  symbol: string,
  netPnlPct: number
): Promise<void> {
  if (!process.env.SENDGRID_API_KEY) {
    console.warn(`[email] SENDGRID_API_KEY not set. Trade closed for ${to}: ${symbol} +${netPnlPct}%`);
    return;
  }
  if (!FROM_EMAIL) {
    console.warn(`[email] MAIL_FROM_EMAIL not set. Trade closed for ${to}: ${symbol} +${netPnlPct}%`);
    return;
  }

  const name        = investorName ?? "Inversor";
  const pctDisplay  = netPnlPct.toFixed(2);
  const year        = new Date().getFullYear();

  const msg: MailDataRequired = {
    to,
    from: { email: FROM_EMAIL, name: FROM_NAME },
    subject: `${PRODUCT_NAME} — Operación cerrada exitosamente en ${symbol} (+${pctDisplay}%)`,
    text: [
      `Hola ${name},`,
      ``,
      `OptiFerre Terminal ha cerrado exitosamente una operación en ${symbol}`,
      `con un rendimiento del +${pctDisplay}%.`,
      ``,
      `Su balance ha sido actualizado. Ingrese a su panel para ver los detalles.`,
      ``,
      `— Equipo ${PRODUCT_NAME}`,
    ].join("\n"),
    html: `
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>${PRODUCT_NAME}</title>
</head>
<body style="margin:0;padding:0;background:#080e16;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#080e16;padding:40px 16px;">
    <tr>
      <td align="center">
        <table width="520" cellpadding="0" cellspacing="0" style="background:#0a1018;border:1px solid #1a2b3c;border-radius:16px;overflow:hidden;">

          <!-- Header band -->
          <tr>
            <td style="background:linear-gradient(135deg,#0d3a26 0%,#0a2c1e 100%);padding:28px 36px;border-bottom:1px solid #12d98b30;">
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td>
                    <p style="margin:0;color:#12d98b;font-size:11px;font-weight:700;letter-spacing:0.14em;text-transform:uppercase;margin-bottom:6px;">
                      Operación cerrada
                    </p>
                    <p style="margin:0;color:#dce7f5;font-size:22px;font-weight:800;letter-spacing:-0.4px;">
                      ${PRODUCT_NAME}
                    </p>
                  </td>
                  <td align="right">
                    <!-- WIN badge -->
                    <div style="background:#12d98b15;border:1px solid #12d98b60;border-radius:8px;padding:8px 16px;display:inline-block;">
                      <p style="margin:0;color:#12d98b;font-size:20px;font-weight:800;font-family:monospace;letter-spacing:1px;">
                        +${pctDisplay}%
                      </p>
                      <p style="margin:2px 0 0;color:#6b8299;font-size:10px;text-transform:uppercase;letter-spacing:0.1em;text-align:center;">
                        rendimiento
                      </p>
                    </div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:32px 36px;">
              <p style="margin:0 0 20px;color:#94a3b8;font-size:14px;line-height:1.6;">
                Hola <strong style="color:#dce7f5;">${name}</strong>,
              </p>
              <p style="margin:0 0 24px;color:#dce7f5;font-size:15px;line-height:1.7;">
                OptiFerre Terminal ha cerrado exitosamente una operación en
                <strong style="color:#57c1ff;font-family:monospace;">${symbol}</strong>
                con un rendimiento del
                <strong style="color:#12d98b;">+${pctDisplay}%</strong>.
              </p>

              <!-- Detail pill -->
              <table width="100%" cellpadding="0" cellspacing="0" style="background:#080e16;border:1px solid #1a2b3c;border-radius:10px;margin-bottom:28px;">
                <tr>
                  <td style="padding:18px 24px;border-right:1px solid #1a2b3c;">
                    <p style="margin:0 0 4px;color:#6b8299;font-size:11px;text-transform:uppercase;letter-spacing:0.1em;">Par</p>
                    <p style="margin:0;color:#57c1ff;font-size:18px;font-weight:700;font-family:monospace;">${symbol}</p>
                  </td>
                  <td style="padding:18px 24px;border-right:1px solid #1a2b3c;">
                    <p style="margin:0 0 4px;color:#6b8299;font-size:11px;text-transform:uppercase;letter-spacing:0.1em;">Resultado</p>
                    <p style="margin:0;color:#12d98b;font-size:18px;font-weight:700;font-family:monospace;">WIN ✓</p>
                  </td>
                  <td style="padding:18px 24px;">
                    <p style="margin:0 0 4px;color:#6b8299;font-size:11px;text-transform:uppercase;letter-spacing:0.1em;">Rendimiento</p>
                    <p style="margin:0;color:#12d98b;font-size:18px;font-weight:700;font-family:monospace;">+${pctDisplay}%</p>
                  </td>
                </tr>
              </table>

              <p style="margin:0 0 28px;color:#94a3b8;font-size:14px;line-height:1.6;">
                Su balance ha sido actualizado. Ingrese a su panel de inversor para consultar los detalles de la operación y el estado actualizado de su portafolio.
              </p>

              <!-- CTA Button -->
              <table cellpadding="0" cellspacing="0" style="margin:0 auto;">
                <tr>
                  <td style="background:#12d98b;border-radius:8px;padding:14px 32px;text-align:center;">
                    <a href="https://tradingdiegomao.datovatenexuspro.com/client/dashboard"
                       style="color:#080e16;font-size:14px;font-weight:700;text-decoration:none;letter-spacing:0.02em;">
                      Ver mi panel →
                    </a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="padding:18px 36px;border-top:1px solid #1a2b3c;">
              <p style="margin:0;color:#334155;font-size:11px;text-align:center;line-height:1.6;">
                Recibiste este correo porque eres inversor activo en ${PRODUCT_NAME}.<br/>
                © ${year} ${PRODUCT_NAME} — Portal de Inversores Institucional
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>`,
    trackingSettings: {
      clickTracking: { enable: false },
      openTracking:  { enable: false },
    },
  };

  await sgMail.send(msg);
}

// ─── sendPammAllocationEmail ───────────────────────────────────────────────────

/**
 * Sends a PAMM allocation notification to a client after their balance is
 * updated by a winning trade close.
 *
 * Shows the client's actual net PnL in USDT (after performance fee and Binance
 * commission) and their new balance — not the generic global PnL%.
 *
 * Errors are NOT swallowed — the caller (PAMM webhook) wraps each send
 * individually via Promise.allSettled so one failure does not block others.
 *
 * @throws {Error} if SendGrid returns a non-2xx status or env vars are absent.
 */
export async function sendPammAllocationEmail(
  to: string,
  investorName: string | null,
  symbol: string,
  netPnlUsdt: number,
  newBalance: number,
): Promise<void> {
  if (!process.env.SENDGRID_API_KEY) {
    console.warn(
      `[email] SENDGRID_API_KEY not set. PAMM win for ${to}: ${symbol} +${netPnlUsdt.toFixed(4)} USDT`,
    );
    return;
  }
  if (!FROM_EMAIL) {
    console.warn(
      `[email] MAIL_FROM_EMAIL not set. PAMM win for ${to}: ${symbol} +${netPnlUsdt.toFixed(4)} USDT`,
    );
    return;
  }

  const name         = investorName ?? "Inversor";
  const pnlDisplay   = netPnlUsdt.toFixed(4);
  const balDisplay   = newBalance.toFixed(2);
  const year         = new Date().getFullYear();

  const msg: MailDataRequired = {
    to,
    from: { email: FROM_EMAIL, name: FROM_NAME },
    subject: `${PRODUCT_NAME} — Operación cerrada en ${symbol} · +${pnlDisplay} USDT acreditados`,
    text: [
      `Hola ${name},`,
      ``,
      `OptiFerre Terminal cerró una operación en ${symbol}.`,
      ``,
      `  Tu ganancia neta:  +${pnlDisplay} USDT`,
      `  Nuevo balance:      ${balDisplay} USDT`,
      ``,
      `Revisa tu panel para ver los detalles completos.`,
      ``,
      `— Equipo ${PRODUCT_NAME}`,
    ].join("\n"),
    html: `
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>${PRODUCT_NAME}</title>
</head>
<body style="margin:0;padding:0;background:#080e16;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#080e16;padding:40px 16px;">
    <tr>
      <td align="center">
        <table width="520" cellpadding="0" cellspacing="0" style="background:#0a1018;border:1px solid #1a2b3c;border-radius:16px;overflow:hidden;">

          <!-- Header band -->
          <tr>
            <td style="background:linear-gradient(135deg,#0d3a26 0%,#0a2c1e 100%);padding:28px 36px;border-bottom:1px solid #12d98b30;">
              <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                  <td>
                    <p style="margin:0;color:#12d98b;font-size:11px;font-weight:700;letter-spacing:0.14em;text-transform:uppercase;margin-bottom:6px;">
                      Operación cerrada
                    </p>
                    <p style="margin:0;color:#dce7f5;font-size:22px;font-weight:800;letter-spacing:-0.4px;">
                      ${PRODUCT_NAME}
                    </p>
                  </td>
                  <td align="right">
                    <div style="background:#12d98b15;border:1px solid #12d98b60;border-radius:8px;padding:8px 16px;display:inline-block;">
                      <p style="margin:0;color:#12d98b;font-size:20px;font-weight:800;font-family:monospace;letter-spacing:1px;">
                        +${pnlDisplay}
                      </p>
                      <p style="margin:2px 0 0;color:#6b8299;font-size:10px;text-transform:uppercase;letter-spacing:0.1em;text-align:center;">
                        USDT neto
                      </p>
                    </div>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:32px 36px;">
              <p style="margin:0 0 20px;color:#94a3b8;font-size:14px;line-height:1.6;">
                Hola <strong style="color:#dce7f5;">${name}</strong>,
              </p>
              <p style="margin:0 0 24px;color:#dce7f5;font-size:15px;line-height:1.7;">
                OptiFerre Terminal cerró una operación en
                <strong style="color:#57c1ff;font-family:monospace;">${symbol}</strong>.
                Tu ganancia neta (después de comisión Binance y fee de gestión) ya fue
                acreditada en tu cuenta.
              </p>

              <!-- Detail pills -->
              <table width="100%" cellpadding="0" cellspacing="0" style="background:#080e16;border:1px solid #1a2b3c;border-radius:10px;margin-bottom:28px;">
                <tr>
                  <td style="padding:18px 24px;border-right:1px solid #1a2b3c;" width="33%">
                    <p style="margin:0 0 4px;color:#6b8299;font-size:11px;text-transform:uppercase;letter-spacing:0.1em;">Par</p>
                    <p style="margin:0;color:#57c1ff;font-size:16px;font-weight:700;font-family:monospace;">${symbol}</p>
                  </td>
                  <td style="padding:18px 24px;border-right:1px solid #1a2b3c;" width="33%">
                    <p style="margin:0 0 4px;color:#6b8299;font-size:11px;text-transform:uppercase;letter-spacing:0.1em;">Ganancia neta</p>
                    <p style="margin:0;color:#12d98b;font-size:16px;font-weight:700;font-family:monospace;">+${pnlDisplay} USDT</p>
                  </td>
                  <td style="padding:18px 24px;" width="34%">
                    <p style="margin:0 0 4px;color:#6b8299;font-size:11px;text-transform:uppercase;letter-spacing:0.1em;">Nuevo balance</p>
                    <p style="margin:0;color:#dce7f5;font-size:16px;font-weight:700;font-family:monospace;">${balDisplay} USDT</p>
                  </td>
                </tr>
              </table>

              <p style="margin:0 0 28px;color:#94a3b8;font-size:14px;line-height:1.6;">
                Revisa tu panel para ver el detalle completo de la operación, el historial
                de rendimiento y el estado actualizado de tu portafolio.
              </p>

              <!-- CTA Button -->
              <table cellpadding="0" cellspacing="0" style="margin:0 auto;">
                <tr>
                  <td style="background:#12d98b;border-radius:8px;padding:14px 32px;text-align:center;">
                    <a href="https://tradingdiegomao.datovatenexuspro.com/client/dashboard"
                       style="color:#080e16;font-size:14px;font-weight:700;text-decoration:none;letter-spacing:0.02em;">
                      Ver mi panel →
                    </a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="padding:18px 36px;border-top:1px solid #1a2b3c;">
              <p style="margin:0;color:#334155;font-size:11px;text-align:center;line-height:1.6;">
                Recibiste este correo porque eres inversor activo en ${PRODUCT_NAME}.<br/>
                La ganancia neta ya incluye el fee de gestión del 5% y la comisión de Binance.<br/>
                © ${year} ${PRODUCT_NAME} — Portal de Inversores Institucional
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>`,
    trackingSettings: {
      clickTracking: { enable: false },
      openTracking:  { enable: false },
    },
  };

  await sgMail.send(msg);
}

// ─── sendDailyCloseSummaryEmail ───────────────────────────────────────────────

type DailyCloseSummary = {
  dayKeyUtc: string;
  trades: number;
  pnl: number;
  service: number;
  clientNet: number;
  capitalStart: number;
  capitalEnd: number;
  firstDayPartial: boolean;
};

/**
 * Sends ONE daily close summary per client.
 * This is the official replacement for per-trade win emails.
 */
export async function sendDailyCloseSummaryEmail(
  to: string,
  investorName: string | null,
  summary: DailyCloseSummary,
): Promise<void> {
  if (!process.env.SENDGRID_API_KEY) {
    console.warn(`[email] SENDGRID_API_KEY not set. Daily close for ${to} ${summary.dayKeyUtc}`);
    return;
  }
  if (!FROM_EMAIL) {
    console.warn(`[email] MAIL_FROM_EMAIL not set. Daily close for ${to} ${summary.dayKeyUtc}`);
    return;
  }

  const name = investorName ?? "Inversor";
  const dayLabel = new Date(`${summary.dayKeyUtc}T00:00:00Z`).toLocaleDateString("es-ES", {
    year: "numeric",
    month: "long",
    day: "2-digit",
    timeZone: "UTC",
  });
  const pnlColor = summary.pnl >= 0 ? "#12d98b" : "#ff6b6b";
  const netColor = summary.clientNet >= 0 ? "#12d98b" : "#ff6b6b";
  const year = new Date().getFullYear();

  const msg: MailDataRequired = {
    to,
    from: { email: FROM_EMAIL, name: FROM_NAME },
    subject: `${PRODUCT_NAME} — Cierre diario ${summary.dayKeyUtc} · ${summary.pnl >= 0 ? "+" : ""}${summary.pnl.toFixed(2)} USDT`,
    text: [
      `Hola ${name},`,
      "",
      `Este es tu cierre diario consolidado (${summary.dayKeyUtc} UTC).`,
      "",
      `Trades cerrados:      ${summary.trades}`,
      `PnL del dia:          ${summary.pnl >= 0 ? "+" : ""}${summary.pnl.toFixed(4)} USDT`,
      `Servicio (20%):       ${summary.service.toFixed(4)} USDT`,
      `Neto cliente:         ${summary.clientNet >= 0 ? "+" : ""}${summary.clientNet.toFixed(4)} USDT`,
      `Capital inicio dia:   ${summary.capitalStart.toFixed(2)} USDT`,
      `Capital cierre dia:   ${summary.capitalEnd.toFixed(2)} USDT`,
      summary.firstDayPartial ? "Nota: dia parcial desde tu hora exacta de alta." : "",
      "",
      "Este correo se envia una sola vez por dia para evitar saturacion.",
      `— Equipo ${PRODUCT_NAME}`,
    ].filter(Boolean).join("\n"),
    html: `
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>${PRODUCT_NAME}</title>
</head>
<body style="margin:0;padding:0;background:#060d17;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#060d17;padding:38px 16px;">
    <tr>
      <td align="center">
        <table width="560" cellpadding="0" cellspacing="0" style="background:#0a1018;border:1px solid #183248;border-radius:16px;overflow:hidden;">
          <tr>
            <td style="background:linear-gradient(135deg,#0f2236 0%,#08202e 100%);padding:28px 34px;border-bottom:1px solid #1f425b;">
              <p style="margin:0;color:#7cc8ff;font-size:11px;font-weight:700;letter-spacing:0.14em;text-transform:uppercase;">Cierre diario consolidado</p>
              <p style="margin:8px 0 0;color:#e6f2ff;font-size:22px;font-weight:800;letter-spacing:-0.4px;">${PRODUCT_NAME}</p>
              <p style="margin:6px 0 0;color:#8aa5bf;font-size:13px;">${dayLabel} (UTC)</p>
            </td>
          </tr>
          <tr>
            <td style="padding:28px 34px;">
              <p style="margin:0 0 18px;color:#9bb7cf;font-size:14px;line-height:1.6;">Hola <strong style="color:#e6f2ff;">${name}</strong>, este es tu resumen diario oficial con liquidacion consolidada.</p>
              <table width="100%" cellpadding="0" cellspacing="0" style="background:#070e17;border:1px solid #1b3246;border-radius:10px;overflow:hidden;margin-bottom:18px;">
                <tr>
                  <td style="padding:14px 16px;border-bottom:1px solid #1b3246;color:#8aa5bf;font-size:12px;">Trades cerrados</td>
                  <td style="padding:14px 16px;border-bottom:1px solid #1b3246;color:#e6f2ff;font-size:13px;font-weight:700;text-align:right;">${summary.trades}</td>
                </tr>
                <tr>
                  <td style="padding:14px 16px;border-bottom:1px solid #1b3246;color:#8aa5bf;font-size:12px;">PnL del dia</td>
                  <td style="padding:14px 16px;border-bottom:1px solid #1b3246;color:${pnlColor};font-size:13px;font-weight:700;text-align:right;">${summary.pnl >= 0 ? "+" : ""}${summary.pnl.toFixed(4)} USDT</td>
                </tr>
                <tr>
                  <td style="padding:14px 16px;border-bottom:1px solid #1b3246;color:#8aa5bf;font-size:12px;">Servicio (20%)</td>
                  <td style="padding:14px 16px;border-bottom:1px solid #1b3246;color:#ffbf47;font-size:13px;font-weight:700;text-align:right;">${summary.service.toFixed(4)} USDT</td>
                </tr>
                <tr>
                  <td style="padding:14px 16px;border-bottom:1px solid #1b3246;color:#8aa5bf;font-size:12px;">Neto cliente</td>
                  <td style="padding:14px 16px;border-bottom:1px solid #1b3246;color:${netColor};font-size:13px;font-weight:700;text-align:right;">${summary.clientNet >= 0 ? "+" : ""}${summary.clientNet.toFixed(4)} USDT</td>
                </tr>
                <tr>
                  <td style="padding:14px 16px;border-bottom:1px solid #1b3246;color:#8aa5bf;font-size:12px;">Capital inicio dia</td>
                  <td style="padding:14px 16px;border-bottom:1px solid #1b3246;color:#3dd6ff;font-size:13px;font-weight:700;text-align:right;">${summary.capitalStart.toFixed(2)} USDT</td>
                </tr>
                <tr>
                  <td style="padding:14px 16px;color:#8aa5bf;font-size:12px;">Capital cierre dia</td>
                  <td style="padding:14px 16px;color:#3dd6ff;font-size:13px;font-weight:700;text-align:right;">${summary.capitalEnd.toFixed(2)} USDT</td>
                </tr>
              </table>
              <p style="margin:0;color:#708ba4;font-size:12px;line-height:1.6;">
                ${summary.firstDayPartial ? "Nota: este dia fue parcial porque inicio desde tu hora exacta de alta. " : ""}
                Este correo se envia una sola vez por dia para evitar congestionar tu bandeja.
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:16px 34px;border-top:1px solid #1b3246;">
              <p style="margin:0;color:#41566a;font-size:11px;text-align:center;line-height:1.6;">© ${year} ${PRODUCT_NAME} · Resumen diario institucional</p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>`,
    trackingSettings: {
      clickTracking: { enable: false },
      openTracking: { enable: false },
    },
  };

  await sgMail.send(msg);
}

// ─── sendBillingStatementEmail ────────────────────────────────────────────────

type BillingStatementSummary = {
  periodStart: string;
  periodEnd: string;
  tradesCount: number;
  pnlUsdt: number;
  serviceDueUsdt: number;
  paidAmountUsdt: number;
  clientNetUsdt: number;
  capitalStartUsdt: number;
  capitalEndUsdt: number;
  status: string;
  paymentNequi: string;
  paymentDaviKey: string;
};

/**
 * Sends a billing statement email for admin collections.
 */
export async function sendBillingStatementEmail(
  to: string,
  investorName: string | null,
  statement: BillingStatementSummary,
): Promise<void> {
  if (!process.env.SENDGRID_API_KEY) {
    console.warn(`[email] SENDGRID_API_KEY not set. Billing statement for ${to} ${statement.periodStart}-${statement.periodEnd}`);
    return;
  }
  if (!FROM_EMAIL) {
    console.warn(`[email] MAIL_FROM_EMAIL not set. Billing statement for ${to} ${statement.periodStart}-${statement.periodEnd}`);
    return;
  }

  const name = investorName ?? "Inversor";
  const pending = Math.max(statement.serviceDueUsdt - statement.paidAmountUsdt, 0);
  const pnlSigned = `${statement.pnlUsdt > 0 ? "+" : ""}${statement.pnlUsdt.toFixed(2)}`;
  const netSigned = `${statement.clientNetUsdt > 0 ? "+" : ""}${statement.clientNetUsdt.toFixed(2)}`;
  const year = new Date().getFullYear();

  const msg: MailDataRequired = {
    to,
    from: { email: FROM_EMAIL, name: FROM_NAME },
    subject: `${PRODUCT_NAME} - Estado de cuenta ${statement.periodStart} a ${statement.periodEnd} - Pendiente ${pending.toFixed(2)} USDT`,
    text: [
      `Hola ${name},`,
      "",
      `Este es tu estado de cuenta de servicio (${statement.periodStart} a ${statement.periodEnd}, UTC).`,
      "",
      `Operaciones cerradas:  ${statement.tradesCount}`,
      `PnL del periodo:       ${pnlSigned} USDT`,
      `Servicio (20%):        ${statement.serviceDueUsdt.toFixed(2)} USDT`,
      `Pagado:                ${statement.paidAmountUsdt.toFixed(2)} USDT`,
      `Saldo pendiente:       ${pending.toFixed(2)} USDT`,
      `Neto cliente:          ${netSigned} USDT`,
      `Capital inicial corte: ${statement.capitalStartUsdt.toFixed(2)} USDT`,
      `Capital final corte:   ${statement.capitalEndUsdt.toFixed(2)} USDT`,
      `Estado:                ${statement.status.toUpperCase()}`,
      "",
      "Canales de pago:",
      `Nequi: ${statement.paymentNequi}`,
      `Llave Davivienda: ${statement.paymentDaviKey}`,
      "",
      "Envia tu soporte de pago para marcar la cuenta como pagada.",
      `- Equipo ${PRODUCT_NAME}`,
    ].join("\n"),
    html: `
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>${PRODUCT_NAME}</title>
</head>
<body style="margin:0;padding:0;background:#050b12;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#050b12;padding:34px 16px;">
    <tr>
      <td align="center">
        <table width="560" cellpadding="0" cellspacing="0" style="background:#0a1018;border:1px solid #1b3246;border-radius:16px;overflow:hidden;">
          <tr>
            <td style="background:linear-gradient(135deg,#13263b 0%,#0b2030 100%);padding:26px 30px;border-bottom:1px solid #21455f;">
              <p style="margin:0;color:#89d6ff;font-size:11px;font-weight:700;letter-spacing:0.12em;text-transform:uppercase;">Estado de cuenta de servicio</p>
              <p style="margin:7px 0 0;color:#e6f2ff;font-size:22px;font-weight:800;letter-spacing:-0.3px;">${PRODUCT_NAME}</p>
              <p style="margin:6px 0 0;color:#8aa5bf;font-size:13px;">Corte: ${statement.periodStart} a ${statement.periodEnd} (UTC)</p>
            </td>
          </tr>
          <tr>
            <td style="padding:28px 30px;">
              <p style="margin:0 0 16px;color:#9bb7cf;font-size:14px;line-height:1.6;">Hola <strong style="color:#e6f2ff;">${name}</strong>, compartimos tu estado de cuenta para control y pago del servicio.</p>

              <table width="100%" cellpadding="0" cellspacing="0" style="background:#07101a;border:1px solid #1b3246;border-radius:10px;overflow:hidden;margin-bottom:16px;">
                <tr><td style="padding:12px 14px;border-bottom:1px solid #1b3246;color:#8aa5bf;font-size:12px;">Operaciones cerradas</td><td style="padding:12px 14px;border-bottom:1px solid #1b3246;color:#e6f2ff;font-size:13px;font-weight:700;text-align:right;">${statement.tradesCount}</td></tr>
                <tr><td style="padding:12px 14px;border-bottom:1px solid #1b3246;color:#8aa5bf;font-size:12px;">PnL del periodo</td><td style="padding:12px 14px;border-bottom:1px solid #1b3246;color:${statement.pnlUsdt >= 0 ? "#19c37d" : "#ff6b6b"};font-size:13px;font-weight:700;text-align:right;">${pnlSigned} USDT</td></tr>
                <tr><td style="padding:12px 14px;border-bottom:1px solid #1b3246;color:#8aa5bf;font-size:12px;">Servicio (20%)</td><td style="padding:12px 14px;border-bottom:1px solid #1b3246;color:#ffbf47;font-size:13px;font-weight:700;text-align:right;">${statement.serviceDueUsdt.toFixed(2)} USDT</td></tr>
                <tr><td style="padding:12px 14px;border-bottom:1px solid #1b3246;color:#8aa5bf;font-size:12px;">Pagado</td><td style="padding:12px 14px;border-bottom:1px solid #1b3246;color:#3dd6ff;font-size:13px;font-weight:700;text-align:right;">${statement.paidAmountUsdt.toFixed(2)} USDT</td></tr>
                <tr><td style="padding:12px 14px;border-bottom:1px solid #1b3246;color:#8aa5bf;font-size:12px;">Saldo pendiente</td><td style="padding:12px 14px;border-bottom:1px solid #1b3246;color:${pending > 0 ? "#ff6b6b" : "#19c37d"};font-size:13px;font-weight:800;text-align:right;">${pending.toFixed(2)} USDT</td></tr>
                <tr><td style="padding:12px 14px;border-bottom:1px solid #1b3246;color:#8aa5bf;font-size:12px;">Neto cliente</td><td style="padding:12px 14px;border-bottom:1px solid #1b3246;color:${statement.clientNetUsdt >= 0 ? "#19c37d" : "#ff6b6b"};font-size:13px;font-weight:700;text-align:right;">${netSigned} USDT</td></tr>
                <tr><td style="padding:12px 14px;border-bottom:1px solid #1b3246;color:#8aa5bf;font-size:12px;">Capital inicio corte</td><td style="padding:12px 14px;border-bottom:1px solid #1b3246;color:#3dd6ff;font-size:13px;font-weight:700;text-align:right;">${statement.capitalStartUsdt.toFixed(2)} USDT</td></tr>
                <tr><td style="padding:12px 14px;color:#8aa5bf;font-size:12px;">Capital cierre corte</td><td style="padding:12px 14px;color:#3dd6ff;font-size:13px;font-weight:700;text-align:right;">${statement.capitalEndUsdt.toFixed(2)} USDT</td></tr>
              </table>

              <table width="100%" cellpadding="0" cellspacing="0" style="background:#070e17;border:1px solid #1b3246;border-radius:10px;overflow:hidden;">
                <tr><td style="padding:12px 14px;border-bottom:1px solid #1b3246;color:#8aa5bf;font-size:12px;">Canal de pago</td><td style="padding:12px 14px;border-bottom:1px solid #1b3246;color:#e6f2ff;font-size:13px;font-weight:700;text-align:right;">Nequi ${statement.paymentNequi}</td></tr>
                <tr><td style="padding:12px 14px;color:#8aa5bf;font-size:12px;">Canal alterno</td><td style="padding:12px 14px;color:#e6f2ff;font-size:13px;font-weight:700;text-align:right;">Llave ${statement.paymentDaviKey}</td></tr>
              </table>

              <p style="margin:14px 0 0;color:#8aa5bf;font-size:12px;line-height:1.6;">Comparte tu soporte de pago para registrar el estado como pagado en el panel administrativo.</p>
            </td>
          </tr>
          <tr>
            <td style="padding:16px 30px;border-top:1px solid #1b3246;">
              <p style="margin:0;color:#43576b;font-size:11px;text-align:center;line-height:1.6;">© ${year} ${PRODUCT_NAME} · Estado de cuenta institucional</p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>`,
    trackingSettings: {
      clickTracking: { enable: false },
      openTracking: { enable: false },
    },
  };

  await sgMail.send(msg);
}
