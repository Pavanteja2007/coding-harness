"use client";

import { useId, useRef, useState } from "react";
import { CommandLine } from "./CommandLine";
import { cn } from "@/lib/cn";

/**
 * WAI-ARIA tabs.
 *
 * Full keyboard support per the APG and MASTER_BUILD_PROMPT §13:
 * ArrowLeft/ArrowRight move, Home/End jump to first/last, and a roving
 * tabIndex means Tab enters the tablist once and then moves on rather than
 * walking through every tab.
 *
 * There is deliberately no "pip install vex" tab. DECISION D2: no PyPI package
 * exists, and inventing that one-liner is named on the DESIGN.md §5 ban list.
 */
export type InstallTab = {
  id: string;
  label: string;
  command: string;
  note?: string;
};

export function InstallTabs({
  tabs,
  className,
}: {
  tabs: InstallTab[];
  className?: string;
}) {
  const [active, setActive] = useState(0);
  const uid = useId();
  const refs = useRef<(HTMLButtonElement | null)[]>([]);

  const focusTab = (i: number) => {
    const next = (i + tabs.length) % tabs.length;
    setActive(next);
    refs.current[next]?.focus();
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    switch (e.key) {
      case "ArrowRight":
        e.preventDefault();
        focusTab(active + 1);
        break;
      case "ArrowLeft":
        e.preventDefault();
        focusTab(active - 1);
        break;
      case "Home":
        e.preventDefault();
        focusTab(0);
        break;
      case "End":
        e.preventDefault();
        focusTab(tabs.length - 1);
        break;
    }
  };

  return (
    <div className={cn("w-full", className)}>
      <div
        role="tablist"
        aria-label="Installation method"
        onKeyDown={onKeyDown}
        className="flex flex-wrap gap-px overflow-hidden rounded-t-lg border border-rule bg-rule"
      >
        {tabs.map((t, i) => {
          const selected = i === active;
          return (
            <button
              key={t.id}
              ref={(el) => {
                refs.current[i] = el;
              }}
              role="tab"
              id={`${uid}-tab-${t.id}`}
              aria-selected={selected}
              aria-controls={`${uid}-panel-${t.id}`}
              tabIndex={selected ? 0 : -1}
              onClick={() => setActive(i)}
              className={cn(
                "min-h-11 flex-1 px-5 py-3 font-mono text-mono transition-colors duration-150 ease-forge",
                selected
                  ? "bg-char text-gilt-bright"
                  : "bg-slab text-smoke hover:text-ash"
              )}
            >
              {t.label}
            </button>
          );
        })}
      </div>

      {tabs.map((t, i) => (
        <div
          key={t.id}
          role="tabpanel"
          id={`${uid}-panel-${t.id}`}
          aria-labelledby={`${uid}-tab-${t.id}`}
          hidden={i !== active}
          className="rounded-b-lg border border-t-0 border-rule bg-char p-5"
        >
          <CommandLine command={t.command} wrap />
          {t.note ? (
            <p className="mt-3 text-small text-smoke">{t.note}</p>
          ) : null}
        </div>
      ))}
    </div>
  );
}
