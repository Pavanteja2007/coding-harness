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
  prominent = false,
  className,
}: {
  command: string;
  /**
   * Hero treatment: larger type, deeper padding, a hotter rule. This is the
   * single most useful object on the landing page for a CLI tool, so it is
   * sized as a headline rather than as an inline snippet.
   */
  prominent?: boolean;
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
        "group flex items-stretch gap-2 rounded-md border bg-char etch",
        prominent ? "border-rule-hot" : "border-rule",
        className
      )}
    >
      <code
        className={cn(
          "min-w-0 flex-1 font-mono text-quench",
          prominent ? "px-6 py-5 text-monolg text-left" : "px-4 py-3 text-mono",
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
        className={cn(
          "flex shrink-0 items-center justify-center self-stretch border-l text-smoke transition-colors duration-150 ease-forge hover:text-ox-bright",
          prominent ? "w-16 border-rule-hot" : "w-12 border-rule"
        )}
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
