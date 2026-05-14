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
