"""Keep pytest from collecting the fixture repos' own test suites — those
are INPUT data for harness end-to-end tests (they contain intentional
bugs), not tests of this project."""
collect_ignore = ["fixtures"]
