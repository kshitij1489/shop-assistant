# External JSON menu

The adapter posts a complete menu into the existing category, item, variant,
and modifier tables. There is no second menu.

## Configure and run

1. Apply migrations. Save commerce settings (commerce can remain disabled) to
   create a location, then create an active POS or menu connection with provider
   `json_menu` and capability `catalog.write`. A menu-only connection does not
   need order submission. One active POS connection per location still applies;
   an existing POS adapter can add `catalog.write` instead of a second connection.
2. In **Menu → Menu source**, choose **External menu**, that connection, and a
   maximum observation age (default 900 seconds). Ordering pauses until a fresh
   snapshot arrives. Switching back to local keeps the current catalog and
   restores dashboard edits. Changing the external authority starts a new
   source generation.
3. Put the connection secret in `COMMERCE_MENU_ADAPTER_SECRET` and read the manifest:

   ```sh
   python -m commerce.adapters.json_menu --base-url https://your-host/commerce \
     --connection YOUR_CONNECTION_UUID --manifest
   ```

4. Export a complete file using `menu_source.generation` from that manifest, then send it:

   ```sh
   python -m commerce.adapters.json_menu --base-url https://your-host/commerce \
     --connection YOUR_CONNECTION_UUID --snapshot /path/to/menu-export.json
   ```

The CLI uses the Python standard library and can run outside this application.
Write the file atomically after a complete export. Schedule exports more often
than the maximum age. Uploading an old file again does not keep ordering enabled.

## Snapshot format

`POST /commerce/v1/connections/{connection_id}/catalog/snapshot/` uses the same
HMAC authentication and 256 KiB request limit. `GET schema/` publishes the
`menu_snapshot` schema. A minimal example:

```json
{
  "schema_version": 1,
  "complete": true,
  "source_generation": "REPLACE_WITH_MANIFEST_GENERATION_UUID",
  "sequence": 1,
  "revision": "menu-export-1",
  "observed_at": "2026-09-22T10:00:00+05:30",
  "currency": "INR",
  "categories": [{"external_id": "drinks", "name": "Drinks", "available": true}],
  "modifier_groups": [{
    "external_id": "milk",
    "name": "Milk",
    "options": [{"external_id": "oat", "name": "Oat milk", "price": "20.00", "available": true}]
  }],
  "items": [{
    "external_id": "latte",
    "name": "Latte",
    "description": "Espresso with steamed milk",
    "available": true,
    "category_id": "drinks",
    "variants": [{"external_id": "regular", "name": "Regular", "price": "150.00", "available": true}],
    "modifier_groups": [{"group_id": "milk", "min_selections": 0, "max_selections": 1, "variant_ids": []}]
  }]
}
```

`observed_at` is when the external source was read, not when a delayed file was
uploaded. `sequence` increases for each new observation of that source
generation. `revision` is a label. The same content still needs a new sequence
and observation time. Retries send the original payload unchanged.

The source currency must equal the tenant commerce currency (INR when commerce
is not configured). Prices are nonnegative decimals with at most two decimal
places. There is no currency conversion. Taxes, discounts, fees, and stock are
configured separately. Menu availability is not a stock count.

## Identity and failures

- Only the selected active `catalog.write` connection can import.
- Categories, groups, and items use external IDs scoped to the connection.
  Variant IDs are scoped to the item. Modifier IDs are scoped to the group.
  Identities are not inferred from names. The first import can reuse an unmapped
  local category with the same unique name. A name that already belongs to
  another mapped category is rejected.
- To keep existing item and variant IDs, create mappings through `mappings/`
  before the first import. Otherwise new external products receive new local IDs.
- An initial snapshot replaces what is offered for sale, including disabling
  local products absent from the file. Omitted items, variants, and modifier
  options are disabled, never deleted. A complete snapshot with no items takes
  the menu off sale.
- Updates are transactional. Invalid prices, references, identities, stale or
  future observations, and database conflicts leave the previous catalog in
  place and do not refresh the success timestamp.
- Reusing a sequence with the same payload returns `unchanged`. Reusing it with
  different data, or sending an older sequence, is rejected. Retries do not
  refresh observation age.
- Local menu edits return HTTP 403 while external mode is on. Per-item knowledge
  notes and existing aliases stay.
- Answers and new checkouts use the synchronized tables and reject a stale menu.
  Accepted orders keep their prices. New prices require basket review and a new
  confirmation.

A change can still happen upstream inside the allowed age window. Menus larger
than 256 KiB cannot be split across requests marked complete. One catalog
authority applies to the tenant.
