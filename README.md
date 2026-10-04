# Shop Assistant

Configurable café/restaurant chatbot for menu help, restaurant knowledge, and
supported ordering across configured channels. Tenants supply knowledge, menu,
checkout policy, and channel credentials.

**Commerce limits (read before live orders):** one checkout location per tenant;
one active connection per role/location; cash or one online payment attempt per
order; exact full capture before expiry; no split tenders/partial captures/gift
cards/tips/FX. See [operator recovery](docs/commerce/operations.md).

## Start here

| Goal | Doc |
| --- | --- |
| Install and run locally | [docs/operations/development.md](docs/operations/development.md) |
| Production and HTTPS | [docs/operations/production.md](docs/operations/production.md) |
| Tests | [docs/operations/testing.md](docs/operations/testing.md) |
| Documentation index | [docs/README.md](docs/README.md) |

Quick Compose demo (seeding is in the development doc):

```sh
cp .env.example .env
# Replace SECRET_KEY, JWT_SECRET, POSTGRES_PASSWORD
docker compose -f docker-compose.yml up --build -d
```

Dashboard: `http://localhost:8080/accounts/login/`. Chat demo needs `OPENAI_API_KEY`.

Live evaluation uses the development stack: `python3 scripts/evaluate-dev up`,
then `python3 scripts/evaluate-dev` for preflight. See [evaluation](docs/evaluate/integration.md).

## Product scope

Bookings, retail, and general non-café workflows are not supported. Adding a
business workflow beyond the café package requires Python changes. Uploaded
prompts configure café answers and existing routes only.

Menus: one local catalog in `orders` tables. **Menu → Menu source** chooses Local
or External. See [menu adapter](docs/commerce/menu_adapter.md) and
[commerce integration](docs/commerce/integration.md).

## Further improvements

Planned work beyond the current café package:

1. **Smarter knowledge injection.** Chunk tenant knowledge and retrieve the
   relevant passages by embedding similarity, so answers use the matching
   context instead of a single bulk prompt.
2. **Dashboard tool control.** Let operators add and remove chatbot tools from
   the UI dashboard, so each tenant can customize the available actions without
   a code change.
3. **Broader business support.** Extend the product past cafés to general
   shop, studio, and retail workflows, while keeping the current café ordering
   path intact.

## License

[MIT](LICENSE).
