import "./globals.css";

export const metadata = {
  title: "OptiFerre Terminal",
  description: "Centro operativo visual para OptiFerre-Trader",
};

export default function RootLayout({ children }) {
  return (
    <html lang="es">
      <body>{children}</body>
    </html>
  );
}