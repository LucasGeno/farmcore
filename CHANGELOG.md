# Changelog

Notable changes for operators and app authors. Newest first.

## Unreleased

- Switching an app off on `/apps` or with `farmcore apps` takes effect at once in every
  process (web, Telegram bot, worker), not at the next start: its pages and writes return 404
  (a fragment already on an open page shows "could not load" until reload), its tools and draft
  kinds disappear, the bot answers that the assistant is off when Chat is off, its background
  work is neither planned nor published and its outbox messages wait until it is switched on
  again. Manifests gain `core_api`, `requires` and `settings`; a durable job is named
  `<app>.<job>`. Migration `core_0014`.
