"use client";

import { useState } from "react";
import { Check, Copy } from "lucide-react";
import { Icon } from "@/components/primitives/Icon";
import { cn } from "@/lib/cn";

/**
 * A copyable one-line command.
 *
 * The copy button writes the command WITHOUT the leading `$`, because that is
 * what a person actually wants on their clipboard. Copying the prompt glyph
 * too is a small, common, and irritating bug.
 *
 * Targets are >=44px tall on touch (DESIGN.md §9).
 */
export function CommandLine({
  command,
  wrap = false,
  className,
}: {
  command: string;
  /**
   * Long commands (a clone URL plus an install) are unreadable when clipped to
   * one line - they look broken even though the copy button still yields the
   * full string. `wrap` lets them run onto a second line instead of being cut.
   */
  wrap?: boolean;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard blocked (insecure context / permissions) - stay silent */
    }
  };

  return (
    <div
      className={cn(
        "group flex items-stretch gap-2 rounded-md border border-rule bg-char etch",
        className
      )}
    >
      <code
        className={cn(
          "min-w-0 flex-1 px-4 py-3 font-mono text-mono text-quench",
          wrap
            ? "whitespace-pre-wrap break-all"
            : "overflow-x-auto whitespace-pre"
        )}
      >
        <span className="select-none text-soot">$&nbsp;</span>
        {command}
      </code>
      <button
        type="button"
        onClick={copy}
        aria-label={copied ? "Copied" : "Copy command"}
        className="flex w-12 shrink-0 items-center justify-center self-stretch border-l border-rule text-smoke transition-colors duration-150 ease-forge hover:text-gilt"
      >
        <Icon
          as={copied ? Check : Copy}
          size={20}
          className={copied ? "text-verdant-bright" : undefined}
        />
      </button>
    </div>
  );
}
