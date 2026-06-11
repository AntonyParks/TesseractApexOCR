import type { Metadata } from "next";
import NavBar from "@/components/NavBar";

export const metadata: Metadata = {
  title: "Apex OCR — Killfeed Leaderboard",
  description: "Apex Legends killfeed ELO leaderboard powered by TesseractApexOCR",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body style={{ margin: 0, background: "#0a0a0a", color: "#e0e0e0", fontFamily: "system-ui, sans-serif", minHeight: "100vh" }}>
        <NavBar />
        <main style={{ maxWidth: "960px", margin: "0 auto", padding: "1.5rem 1rem" }}>
          {children}
        </main>
      </body>
    </html>
  );
}
