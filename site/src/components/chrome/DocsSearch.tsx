"use client";

/**
 * Docs command palette - opens on Cmd+K / Ctrl+K, or on the nav trigger.
 *
 * Accessibility is handled the same way Nav.tsx handles the mobile sheet, and
 * for the same reason: the sheet's behaviour was verified by real keyboard
 * input, so matching it keeps one pattern to reason about rather than two.
 *   - Esc closes, focus returns to the trigger, body scroll unlocks - all in
 *     the effect cleanup, so every exit path gets the same treatment
 *   - Tab is trapped across the panel's only two focusables (input, close)
 *
 * cmdk's own <Command.Dialog> is deliberately NOT used: it brings a Radix
 * dialog with its own focus trap and scroll lock, which would mean two
 * competing implementations of the behaviour Nav already establishes here.
 *
 * Matching runs over title, summary and section through cmdk's default
 * scorer. `value` stays the slug so it is stable across renders, and the
 * prose fields ride along as `keywords` - the documented way to widen a
 * match without making the item's identity depend on its copy.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Command } from "cmdk";
import { Search, X } from "lucide-react";
import { DOCS, DOC_SECTIONS } from "@/lib/content/docs";
import { Icon } from "@/components/primitives/Icon";
import { cn } from "@/lib/cn";

const FOCUSABLE =
  'a[href], button:not([disabled]), input, select, textarea, [tabindex]:not([tabindex="-1"])';

export function DocsSearch() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  // Set synchronously by openSearch, so the panel's FIRST render already knows
  // whether it may animate. A post-mount check would flash one animated frame.
  const [reduced, setReduced] = useState(false);
  const [shown, setShown] = useState(false);
  // Cmd on Apple, Ctrl elsewhere. Server-rendered as the Cmd glyph and
  // corrected after mount - platform is not knowable during SSR, and guessing
  // in markup is a hydration mismatch.
  const [isMac, setIsMac] = useState(true);

  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const openSearch = useCallback(() => {
    const r = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    setReduced(r);
    setShown(r); // under reduced motion, mount already at the final state
    setSearch("");
    setOpen(true);
  }, []);

  useEffect(() => {
    setIsMac(/Mac|iPhone|iPad|iPod/i.test(navigator.userAgent));
  }, []);

  // The global shortcut. Mounted whether or not the palette is open.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key?.toLowerCase() !== "k" || !(e.metaKey || e.ctrlKey)) return;
      e.preventDefault(); // beats the browser's own Ctrl+K search box
      openSearch();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [openSearch]);

  useEffect(() => {
    if (!open) return;

    inputRef.current?.focus();

    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    let raf = 0;
    if (!reduced) raf = requestAnimationFrame(() => setShown(true));

    const panel = panelRef.current;

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        setOpen(false);
        return;
      }
      if (e.key !== "Tab" || !panel) return;

      const items = [...panel.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
        (el) => el.offsetParent !== null
      );
      if (!items.length) return;

      const firstEl = items[0];
      const lastEl = items[items.length - 1];

      if (e.shiftKey && document.activeElement === firstEl) {
        e.preventDefault();
        lastEl.focus();
      } else if (!e.shiftKey && document.activeElement === lastEl) {
        e.preventDefault();
        firstEl.focus();
      }
    };

    document.addEventListener("keydown", onKey);
    return () => {
      if (raf) cancelAnimationFrame(raf);
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
      setShown(false);
      // Focus returns to the trigger, never to <body>.
      triggerRef.current?.focus?.();
    };
  }, [open, reduced]);

  const onSelect = useCallback(
    (slug: string) => {
      setOpen(false);
      router.push(`/docs/${slug}`);
    },
    [router]
  );

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={openSearch}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label="Search documentation"
        className="inline-flex min-h-9 cursor-pointer select-none items-center gap-2 rounded-md border border-rule bg-char px-3 text-small text-ash etch transition-colors duration-150 ease-forge hover:bg-forge hover:text-quench"
      >
        <Icon as={Search} size={20} className="h-4 w-4" />
        <span className="font-sans">Search</span>
        {/* Hidden where there is no pointer to hover with - a phone has no Cmd+K. */}
        <kbd className="ml-1 hidden font-mono text-eyebrow tracking-[0.08em] text-smoke pointer-fine:inline">
          {isMac ? "⌘K" : "Ctrl K"}
        </kbd>
      </button>

      {open ? (
        <div className="fixed inset-0 z-[70] flex items-start justify-center p-4 pt-[12vh] sm:p-6 sm:pt-[14vh]">
          <div
            aria-hidden="true"
            onClick={() => setOpen(false)}
            className="absolute inset-0 bg-ink/70 backdrop-blur-md"
          />

          <div
            ref={panelRef}
            role="dialog"
            aria-modal="true"
            aria-label="Search documentation"
            className={cn(
              "relative flex w-full max-w-[620px] flex-col overflow-hidden rounded-lg border border-rule bg-slab etch",
              !reduced &&
                "transition-[opacity,transform] duration-150 ease-forge",
              shown ? "translate-y-0 opacity-100" : "-translate-y-1 opacity-0"
            )}
          >
            <Command
              label="Search documentation"
              loop
              className="flex min-h-0 flex-col"
            >
              <div className="flex items-center gap-3 border-b border-rule px-4">
                <Icon as={Search} size={20} />
                <Command.Input
                  ref={inputRef}
                  value={search}
                  onValueChange={setSearch}
                  placeholder="Search the docs"
                  className="min-w-0 flex-1 bg-transparent py-4 font-sans text-body text-quench placeholder:text-smoke focus:outline-none"
                />
                <button
                  type="button"
                  onClick={() => setOpen(false)}
                  aria-label="Close search"
                  className="flex h-9 w-9 shrink-0 cursor-pointer items-center justify-center rounded-md text-ash transition-colors duration-150 ease-forge hover:text-quench"
                >
                  <Icon as={X} size={20} />
                </button>
              </div>

              <Command.List className="max-h-[min(56vh,420px)] overflow-y-auto overscroll-contain p-2">
                <Command.Empty className="px-3 py-10 text-center">
                  <p className="text-body text-ash">
                    No pages match{" "}
                    <span className="font-mono text-ox-bright">
                      {search.trim() || "that"}
                    </span>
                  </p>
                  <p className="mt-2 text-small text-smoke">
                    Try a command name, a concept, or part of a page title.
                  </p>
                </Command.Empty>

                {DOC_SECTIONS.map((section) => {
                  const pages = DOCS.filter((d) => d.section === section);
                  if (!pages.length) return null;
                  return (
                    <Command.Group
                      key={section}
                      heading={section}
                      className="mb-1 [&_[cmdk-group-heading]]:px-3 [&_[cmdk-group-heading]]:py-2 [&_[cmdk-group-heading]]:font-mono [&_[cmdk-group-heading]]:text-mono [&_[cmdk-group-heading]]:text-soot"
                    >
                      {pages.map((d) => (
                        <Command.Item
                          key={d.slug}
                          value={d.slug}
                          keywords={[d.title, d.summary, d.section]}
                          onSelect={onSelect}
                          className="flex cursor-pointer flex-col gap-1 rounded-md border border-transparent px-3 py-2.5 data-[selected=true]:border-rule data-[selected=true]:bg-char"
                        >
                          <span className="flex items-baseline justify-between gap-3">
                            <span className="text-body text-quench">
                              {d.title}
                            </span>
                            <span className="shrink-0 font-mono text-eyebrow text-soot">
                              /docs/{d.slug}
                            </span>
                          </span>
                          <span className="line-clamp-2 text-small text-ash">
                            {d.summary}
                          </span>
                        </Command.Item>
                      ))}
                    </Command.Group>
                  );
                })}
              </Command.List>

              <div className="flex items-center justify-between gap-4 border-t border-rule bg-char px-4 py-2.5">
                <span className="font-mono text-eyebrow text-soot">
                  {DOCS.length} pages
                </span>
                <span className="flex items-center gap-3 font-mono text-eyebrow text-soot">
                  <span>&uarr;&darr; navigate</span>
                  <span>&crarr; open</span>
                  <span>esc close</span>
                </span>
              </div>
            </Command>
          </div>
        </div>
      ) : null}
    </>
  );
}
