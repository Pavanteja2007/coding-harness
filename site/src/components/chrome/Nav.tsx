"use client";

/**
 * Sticky nav - the ONE glass surface on the site (DESIGN.md §5 ban list:
 * "Glassmorphism / backdrop-blur cards. Nav bar only, and subtly").
 *
 * Collapses to a full-screen sheet below 820px. The sheet traps focus, closes
 * on Esc, locks body scroll, and returns focus to the trigger. All of that is
 * verified by real keyboard input, not assumed.
 */
import { useEffect, useRef, useState } from "react";
import { Menu, X } from "lucide-react";
import { site } from "@/lib/site";
import { ButtonLink } from "@/components/primitives/Button";
import { Icon } from "@/components/primitives/Icon";

const FOCUSABLE =
  'a[href], button:not([disabled]), input, select, textarea, [tabindex]:not([tabindex="-1"])';

export function Nav() {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const sheetRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;

    const sheet = sheetRef.current;
    const first = sheet?.querySelector<HTMLElement>(FOCUSABLE);
    first?.focus();

    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        setOpen(false);
        return;
      }
      if (e.key !== "Tab" || !sheet) return;

      const items = [...sheet.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
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
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
      // Focus returns to the trigger, never to <body>.
      triggerRef.current?.focus?.();
    };
  }, [open]);

  return (
    <>
      <header className="sticky top-0 z-50 border-b border-rule bg-ink/80 backdrop-blur-md">
        <nav
          aria-label="Main"
          className="mx-auto flex h-16 max-w-[1200px] items-center justify-between gap-6 px-6"
        >
          <a
            href="/"
            className="font-display text-[1.6rem] leading-none lowercase text-quench"
          >
            vex
          </a>

          <div className="hidden items-center gap-7 min-[820px]:flex">
            {site.navLinks.map((l) => (
              <a
                key={l.href}
                href={l.href}
                className="text-small text-ash transition-colors duration-150 ease-forge hover:text-quench"
              >
                {l.label}
              </a>
            ))}
            <a
              href={site.repo}
              target="_blank"
              rel="noreferrer noopener"
              className="text-small text-ash transition-colors duration-150 ease-forge hover:text-quench"
            >
              GitHub
            </a>
            <ButtonLink variant="gilt" size="sm" href="#install">
              Install
            </ButtonLink>
          </div>

          <button
            ref={triggerRef}
            type="button"
            onClick={() => setOpen(true)}
            aria-expanded={open}
            aria-controls="mobile-nav"
            aria-label="Open menu"
            className="flex h-11 w-11 items-center justify-center rounded-md text-ash transition-colors duration-150 ease-forge hover:text-quench min-[820px]:hidden"
          >
            <Icon as={Menu} size={24} />
          </button>
        </nav>
      </header>

      {open ? (
        <div
          ref={sheetRef}
          id="mobile-nav"
          role="dialog"
          aria-modal="true"
          aria-label="Menu"
          className="fixed inset-0 z-[60] flex flex-col bg-ink min-[820px]:hidden"
        >
          <div className="flex h-16 items-center justify-between border-b border-rule px-6">
            <span className="font-display text-[1.6rem] leading-none lowercase text-quench">
              vex
            </span>
            <button
              type="button"
              onClick={() => setOpen(false)}
              aria-label="Close menu"
              className="flex h-11 w-11 items-center justify-center rounded-md text-ash hover:text-quench"
            >
              <Icon as={X} size={24} />
            </button>
          </div>

          <div className="flex flex-1 flex-col gap-1 px-6 py-8">
            {site.navLinks.map((l) => (
              <a
                key={l.href}
                href={l.href}
                onClick={() => setOpen(false)}
                className="py-3 text-h3 text-quench"
              >
                {l.label}
              </a>
            ))}
            <a
              href={site.repo}
              target="_blank"
              rel="noreferrer noopener"
              className="py-3 text-h3 text-quench"
            >
              GitHub
            </a>
            <ButtonLink
              variant="gilt"
              size="lg"
              href="#install"
              className="mt-4 self-start"
              onClick={() => setOpen(false)}
            >
              Install
            </ButtonLink>
          </div>
        </div>
      ) : null}
    </>
  );
}
