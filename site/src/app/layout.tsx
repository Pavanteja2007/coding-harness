import type { Metadata } from "next";
import { Fraunces, Archivo, JetBrains_Mono } from "next/font/google";
import { site } from "@/lib/site";
import { SkipLink } from "@/components/primitives/SkipLink";
import { Nav } from "@/components/chrome/Nav";
import { Footer } from "@/components/chrome/Footer";
import "./globals.css";

/**
 * Fraunces carries opsz / SOFT / WONK. `wght` is included by default and must
 * NOT be listed in `axes` - next/font rejects it. DESIGN.md §2.1.
 */
const fraunces = Fraunces({
  subsets: ["latin"],
  display: "swap",
  axes: ["SOFT", "WONK", "opsz"],
  variable: "--font-fraunces",
});

const archivo = Archivo({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-archivo",
});

const jetbrains = JetBrains_Mono({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-jetbrains",
});

export const metadata: Metadata = {
  title: {
    default: site.tagline,
    template: "%s · vex",
  },
  description: site.description,
  metadataBase: new URL(site.url),
  openGraph: {
    title: site.tagline,
    description: site.description,
    type: "website",
    siteName: "vex",
  },
  twitter: {
    card: "summary_large_image",
    title: site.tagline,
    description: site.description,
  },
};

export const viewport = {
  width: "device-width",
  initialScale: 1,
  colorScheme: "dark" as const,
  themeColor: "#080706",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html
      lang="en"
      className={`${fraunces.variable} ${archivo.variable} ${jetbrains.variable}`}
    >
      <body>
        <SkipLink />
        <Nav />
        <main id="main">{children}</main>
        <Footer />
      </body>
    </html>
  );
}
