import type { MetadataRoute } from "next";
import { site } from "@/lib/site";
import { DOCS } from "@/lib/content/docs";

/**
 * The sitemap.
 *
 * Generated from the same sources the routes are, so a new docs page appears
 * here automatically. An earlier version listed only "/" — every other route,
 * including all 16 documentation pages, was invisible to crawlers, which
 * quietly defeated the rest of the SEO work.
 *
 * Priorities are relative, not absolute: the landing page outranks the
 * reference pages, and getting-started outranks deep reference.
 */
export default function sitemap(): MetadataRoute.Sitemap {
  const now = new Date();

  const top: MetadataRoute.Sitemap = [
    { url: site.url, lastModified: now, changeFrequency: "weekly", priority: 1 },
    { url: `${site.url}/docs`, lastModified: now, changeFrequency: "weekly", priority: 0.9 },
    { url: `${site.url}/architecture`, lastModified: now, changeFrequency: "monthly", priority: 0.8 },
    { url: `${site.url}/benchmarks`, lastModified: now, changeFrequency: "monthly", priority: 0.8 },
    { url: `${site.url}/changelog`, lastModified: now, changeFrequency: "weekly", priority: 0.6 },
    { url: `${site.url}/about`, lastModified: now, changeFrequency: "monthly", priority: 0.6 },
  ];

  const docs: MetadataRoute.Sitemap = DOCS.map((d) => ({
    url: `${site.url}/docs/${d.slug}`,
    lastModified: now,
    changeFrequency: "monthly" as const,
    // Getting started matters more to a new reader than deep reference.
    priority: d.section === "Getting started" ? 0.8 : 0.7,
  }));

  return [...top, ...docs];
}
