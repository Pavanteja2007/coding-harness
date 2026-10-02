import type { Metadata } from "next";
import { Bodoni_Moda, Schibsted_Grotesk, Azeret_Mono } from "next/font/google";
import { site } from "@/lib/site";
import { SkipLink } from "@/components/primitives/SkipLink";
import { Nav } from "@/components/chrome/Nav";
import { ScrollProgress } from "@/components/motion/ScrollProgress";
import { Footer } from "@/components/chrome/Footer";
import "./globals.css";

/**
 * Type. Chosen against the category, not with it.
 *
 * Bodoni Moda is a didone - the letterform of engraved banknotes, plate
 * lettering and luxury houses. Extreme stroke contrast, and essentially unused
 * in developer tooling, which is exactly why it reads as expensive here rather
 * than as another Fraunces/Inter landing page.
 *
 * Schibsted Grotesk is a Norwegian editorial grotesk: sturdier and less
 * ubiquitous than Inter or Archivo.
 *
 * Azeret Mono is squarer and more mechanical than JetBrains Mono, which has
 * become the default "developer" mono everywhere.
 */
const bodoni = Bodoni_Moda({
  subsets: ["latin"],
  display: "swap",
  axes: ["opsz"],
  variable: "--font-bodoni",
});

const schibsted = Schibsted_Grotesk({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-schibsted",
});

const azeret = Azeret_Mono({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-azeret",
});

export const metadata: Metadata = {
  title: {
    default: site.tagline,
    template: "%s · neo",
  },
  description: site.description,
  // metadataBase is what makes every relative URL in the tree (canonical,
  // opengraph-image, alternates.languages) resolve to an absolute one.
  metadataBase: new URL(site.url),
  // NOTE: deliberately no root-level `alternates.canonical`. It is
  // INHERITED by every nested route, so a canonical of "/" here would tell
  // a crawler that all ~25 pages are duplicates of the homepage. Each route
  // declares its own via `site.canonical()`.
  openGraph: {
    title: site.tagline,
    description: site.description,
    type: "website",
    siteName: "neo",
    url: site.url,
    locale: "en_US",
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
  themeColor: "#09090B",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html
      lang="en"
      className={`${bodoni.variable} ${schibsted.variable} ${azeret.variable}`}
    >
      <body>
        {/* JSON-LD. Only fields the repo actually supports: no ratings, no
            install counts, no fabricated authorship. */}
        <script
          type="application/ld+json"
          dangerouslySetInnerHTML={{
            __html: JSON.stringify({
              "@context": "https://schema.org",
              "@type": "SoftwareApplication",
              name: "neo",
              applicationCategory: "DeveloperApplication",
              operatingSystem: "Linux, macOS, Windows",
              description: site.description,
              url: site.repo,
              license: "https://opensource.org/licenses/MIT",
              programmingLanguage: "Python",
            }),
          }}
        />
        <SkipLink />
        <ScrollProgress />
        <Nav />
        <main id="main">{children}</main>
        <Footer />
      </body>
    </html>
  );
}
