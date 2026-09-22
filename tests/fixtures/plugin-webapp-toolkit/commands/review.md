# Review the code $ARGUMENTS in this repo

You are acting as a careful senior reviewer, then a fixer. First REVIEW,
then FIX what the review finds:

1. Read the code the arguments name ($ARGUMENTS); if no arguments were
   given, review the files the most recent failing test exercises.
2. Check for: correctness bugs (boundary conditions, off-by-one, wrong
   operator), silent-failure paths (swallowed exceptions, missing
   guards), and obvious regressions the current tests would miss.
3. For each finding, decide: is it THE reported bug, adjacent damage,
   or pre-existing? Fix only what the report covers; list the rest in
   your final summary instead of fixing.
4. Apply the minimal fix for the reported bug; keep edits focused.

The harness verifies with the repo's test suite — success is gated on
the target test passing plus no regressions, not on your summary.
