"""Fake apps for module lifecycle tests, loaded by a real `farmcore worker` process too.

`beacon` has a page, a tool, a durable task and an installation-wide job; `lamp` delivers outbox
intents. Their callables report to files under `$LIFECYCLE_DIR`, which the test reads.
"""
