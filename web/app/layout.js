import "./globals.css";

export const metadata = {
  title: "OptiFerre Terminal",
  description: "Centro operativo visual para OptiFerre-Trader",
};

export const viewport = {
  width: "device-width",
  initialScale: 1,
};

// Script inline: captura ChunkLoadError antes de que React lo procese.
// Cuando hay un deploy nuevo los hashes de chunks cambian — el navegador
// intenta cargar chunks del build anterior (ya inexistentes) y falla.
// La solución: detectar el error y recargar una sola vez con cache-bust.
const chunkErrorScript = `
(function() {
  var RELOAD_KEY = '__chunk_reload_ts';
  function isChunkError(msg) {
    return /loading chunk|failed to fetch dynamically imported|loading css chunk/i.test(msg || '');
  }
  window.addEventListener('error', function(e) {
    if (isChunkError(e.message)) {
      var last = parseInt(sessionStorage.getItem(RELOAD_KEY) || '0');
      if (Date.now() - last > 10000) {
        sessionStorage.setItem(RELOAD_KEY, Date.now());
        window.location.reload();
      }
    }
  });
  window.addEventListener('unhandledrejection', function(e) {
    var msg = (e.reason && (e.reason.message || String(e.reason))) || '';
    if (isChunkError(msg)) {
      var last = parseInt(sessionStorage.getItem(RELOAD_KEY) || '0');
      if (Date.now() - last > 10000) {
        sessionStorage.setItem(RELOAD_KEY, Date.now());
        window.location.reload();
      }
    }
  });
})();
`;

export default function RootLayout({ children }) {
  return (
    <html lang="es">
      <head>
        <script dangerouslySetInnerHTML={{ __html: chunkErrorScript }} />
      </head>
      <body>{children}</body>
    </html>
  );
}