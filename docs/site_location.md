# Café site location

**Settings → Business details** stores street address 1 (required), street
address 2, city, state, country, and postal code (required). Search a city,
optionally with a country name, choose a suggestion, then enter the street and
postal code. State and country come from the selected city. State may be empty
when the directory has none. Saving an unchanged address skips geocoder calls.
WhatsApp settings have their own save button and do not look up a location.

Customer delivery addresses are separate. They are typed text plus coverage
rules, and they are not sent to Photon or Nominatim. See [checkout](chatbot/checkout.md).

## Services

No Google key is required for this form.

- **Photon** (`PHOTON_BASE_URL`) provides city autocomplete. Suggestions carry OpenStreetMap IDs.
- **Nominatim** (`NOMINATIM_BASE_URL`) resolves the selected ID and checks the postal code in that country. Street addresses are not sent.

```dotenv
PHOTON_BASE_URL=https://photon.example.com
NOMINATIM_BASE_URL=https://nominatim.example.com
```

No public endpoint is used by default. `NOMINATIM_API_KEY`, `NOMINATIM_USER_AGENT`,
and the timeout settings also apply. The default user agent is
`StudioDesk/1.0 (site postal directory)`.

The postal check needs the full code plus matching city, state, and country. If
the city has no state, it uses city, country, and the full code. A confirmed
mismatch shows “Enter a valid postal code for the city.” Incomplete results show
“We could not verify this postal code for the selected city.” The saved address
stays unchanged. Outages show a retry error. OpenStreetMap is not a postal
authority: valid codes can be missing.

Autocomplete starts after two characters. Photon results are cached for five
minutes. Approved tenants may make up to 60 city lookups per minute. The UI
attributes OpenStreetMap contributors.

Do not use the public Nominatim service for autocomplete. Its
[usage policy](https://operations.osmfoundation.org/policies/nominatim/) forbids
that and limits the application to one request per second. Autocomplete calls
Photon only. If you set `NOMINATIM_BASE_URL` to the public host, every worker
must share `APP_REDIS_URL` with a `noeviction` policy. One request is in flight
per host, then workers wait at least one second. A 429 applies `Retry-After`,
or 60 seconds when the header is missing. If a killed worker leaves the host
closed:

1. Stop every worker using that Redis database.
2. After in-flight calls and any cooldown have finished, delete only `geocoding:nominatim:<hostname>:owner`. Keep the cooldown key. Do not flush Redis.
3. Wait at least the configured interval, then start workers.

Prefer a hosted or self-hosted directory for production. Photon's public demo
is not a configured default. A Google place id is not a city selection; choose
the city again from the suggestions, and the stored address stays until that
save succeeds. Changing the free-text address in the master directory clears
the previously validated street, city, state, country, and postal components.

Provider references: [Photon](https://github.com/komoot/photon),
[Photon API](https://github.com/komoot/photon/blob/master/docs/api-v1.md),
[Nominatim lookup](https://nominatim.org/release-docs/latest/api/Lookup/).
