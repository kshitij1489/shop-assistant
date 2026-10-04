# mock_services/location/

**Responsible for:** Loopback geocoding emulator behind
`LOCATION_PROVIDER=emulator`.

**Not responsible for:** Delivery coverage rules (`serviceable_pincodes`) or
Google Maps production traffic.

## Read first

- Emulator routes under the shared `mock_services` server
- `test_location.py`
- Application seam: `chatbot_core/logic/cafe/location_provider.py`

## Docs

- [Local simulators](../../docs/mock_services/overview.md)

## Verify

```sh
python -m unittest mock_services.location.test_location -v
python manage.py test tests.integration.test_location_provider \
  --settings=tests.settings.integration -v 2
```
