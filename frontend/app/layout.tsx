import "./globals.css";
import type { Metadata } from "next";
import ClientShell from "./client-shell";

export const metadata: Metadata = {
  title: "AMTA — AI Master Trading Agent",
  description: "Autonomous Kalshi paper trading agent",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <ClientShell>{children}</ClientShell>
      </body>
    </html>
  );
}
