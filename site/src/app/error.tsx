"use client";

import { useEffect } from "react";
import { Button, ButtonLink } from "@/components/primitives/Button";

/**
 * Route-level error boundary.
 *
 * Without this, any render throw shows a blank white screen — no nav, no way
 * back, no indication anything is recoverable. Next requires the boundary to
 * be a client component.
 *
 * The reset() prop retries the segment, which is the right first move: a
 * transient failure often clears. If it does not, the links out are there.
 *
 * Per the voice rules this does not apologise and is not vague about what
 * happened. The digest is surfaced because it is the one thing that makes a
 * production error reportable.
 */
export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // Server-side errors arrive with only a digest; the message is withheld in
    // production. Logging keeps it findable in the browser console.
    console.error(error);
  }, [error]);

  return (
    <section className="mx-auto flex min-h-[70svh] max-w-[1200px] flex-col justify-center px-6 py-24">
      <div className="max-w-[52rem]">
        <div className="mb-6 flex items-center gap-3">
          <span className="font-mono text-mono uppercase tracking-[0.16em] text-warn">
            error
          </span>
          <span aria-hidden="true" className="h-px w-16 bg-rule-hot" />
        </div>

        <h1 className="mb-6 max-w-[18ch] text-h1 text-quench">
          Something failed while rendering this page.
        </h1>

        <p className="mb-10 max-w-[54ch] text-lead text-ash">
          Retrying often clears it. If it keeps happening, the digest below
          identifies this specific failure in the logs.
        </p>

        <div className="mb-10 flex flex-wrap gap-3">
          <Button variant="ox" size="lg" onClick={reset}>
            Try again
          </Button>
          <ButtonLink variant="secondary" size="lg" href="/">
            Back to the start
          </ButtonLink>
        </div>

        {error.digest ? (
          <div className="rounded-lg border border-rule border-l-2 border-l-warn bg-char p-5">
            <h2 className="mb-2 font-mono text-mono text-warn">
              Error digest
            </h2>
            <code className="font-mono text-mono text-ash">{error.digest}</code>
          </div>
        ) : null}
      </div>
    </section>
  );
}
