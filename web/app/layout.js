import "./globals.css";

export const metadata = {
  title: "OptiFerre Terminal",
  description: "Centro operativo visual para OptiFerre-Trader",
};

export const viewport = {
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }) {
  return (
    <html lang="es">
      <body>{children}</body>
    </html>
  );
}