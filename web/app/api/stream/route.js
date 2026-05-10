// Server-Sent Events endpoint — empuja estado del bot cada 5 s sin polling desde el cliente.
// Uso: const es = new EventSource('/api/stream'); es.onmessage = (e) => setPayload(JSON.parse(e.data));
import { readDashboardState } from "../../../lib/read-dashboard-state";

export const dynamic = "force-dynamic";

export async function GET() {
  const encoder = new TextEncoder();

  const stream = new ReadableStream({
    async start(controller) {
      let closed = false;

      const send = async () => {
        if (closed) return;
        try {
          const data = await readDashboardState();
          controller.enqueue(encoder.encode(`data: ${JSON.stringify(data)}\n\n`));
        } catch {
          // ignorar errores de lectura de archivo
        }
      };

      // Enviar estado inicial inmediatamente
      await send();

      // Luego cada 5 s
      const interval = setInterval(send, 5000);

      // Heartbeat cada 30 s para mantener la conexión viva en proxies
      const heartbeat = setInterval(() => {
        if (!closed) {
          try {
            controller.enqueue(encoder.encode(": ping\n\n"));
          } catch {
            /* conexión cerrada */
          }
        }
      }, 30000);

      // Limpieza cuando el cliente desconecta (el controller se cierra externamente)
      return () => {
        closed = true;
        clearInterval(interval);
        clearInterval(heartbeat);
      };
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no", // Coolify / Nginx: deshabilitar buffering para SSE
    },
  });
}
