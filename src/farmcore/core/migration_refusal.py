"""A migration's refusal: the schema was not changed, and the message says why.

A data-changing revision that finds rows it must not coerce (a preflight) or a result that does
not add up (a post-check) raises `MigrationRefused`. Its message names ids and counts only, never
row values, so `farmcore migrate` and `farmcore upgrade` print it as it is; any other failure is
reported by its type alone (`commands.migrate._failure`)."""


class MigrationRefused(RuntimeError):
    """Raised inside a migration's transaction: everything it did is rolled back."""
