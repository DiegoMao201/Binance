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
