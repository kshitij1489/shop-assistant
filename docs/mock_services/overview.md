# Local payment and POS simulators

`mock_services` is a loopback stand-in for menu import, payment, and POS. It
moves no real money, has no authentication, and must stay on an isolated host.
Python 3.10 or newer. The standard library is enough to run it.

## Offline demo

```sh
python3 -m mock_services.commerce_adapter demo
```

The demo prints `PASS` for a capture, a duplicate delivery, and a timeout after
a side effect. It writes SQLite files under a new directory in
`/tmp/commerce-adapter-demo`. It does not call a live Studio Desk checkout.

## HTTP simulators

```sh
python3 -m mock_services serve
```

Menu, payment, POS, and a location emulator listen at `http://127.0.0.1:9080`.
`GET /health` is readiness. `GET /v1/menu` is the synthetic catalog. The default
bind is loopback. `0.0.0.0` is for Compose.

To attach them to a disposable tenant:

1. In **Commerce settings**, use currency INR and online checkout. Create two active `custom` / `test` connections:

   | Role | Capabilities |
   | --- | --- |
   | Payment | `payment.create`, `payment.reconcile` |
   | POS and menu | `order.submit`, `order.reconcile`, `catalog.write` |

   The mock account is the connection UUID. One active POS connection per location.

2. In **Menu → Menu source**, choose **External menu** and the POS connection. Sync more often than the menu maximum age (default 900 seconds):

   ```sh
   export COMMERCE_ADAPTER_SECRET='<POS connection signing secret>'
   python3 -m mock_services sync-menu \
     --base-url https://your-test-host/commerce --connection '<POS connection UUID>'
   ```

3. Assign local numeric stock in **Commerce settings → Stock**. Menu availability is not stock.

4. Start one worker per connection. Omit `--auto-capture` when you need to capture or fail a payment yourself.

   ```sh
   export COMMERCE_ADAPTER_SECRET='<payment connection signing secret>'
   python3 -m mock_services adapter --role payment \
     --base-url https://your-test-host/commerce --connection '<payment connection UUID>' \
     --db mock_services/.state/payment.sqlite3 --auto-capture
   ```

   Use `--role pos`, the POS secret, and a separate `--db` for the POS worker.
   Studio Desk still requires HTTPS for adapter calls. Keep
   `reconcile_commerce` scheduled. See [commerce integration](../commerce/integration.md).

The same package can run the reference adapter directly:

```sh
python3 -m mock_services.commerce_adapter run \
  --base-url https://your-test-host/commerce \
  --connection '<payment connection UUID>' \
  --db /tmp/reference-payment.sqlite3 \
  --provider-db /tmp/reference-provider.sqlite3 --port 8766
```

Set `COMMERCE_ADAPTER_SECRET` to that connection's signing secret and
`FAKE_WEBHOOK_SECRET` to a local webhook secret. Live connections are rejected.
`capture` and `inspect` are documented by
`python3 -m mock_services.commerce_adapter --help`.

The location emulator in this process is for evaluation fixtures only.
`LOCATION_EMULATOR_URL` must be a loopback `http` origin. Customer delivery
addresses and production café lookup do not use it. See
[site location](../site_location.md).

The payment simulator does not advertise `payment.refund` and does not change
`refunded_minor`. It has no `authorized` state and no partial capture. POS
orders stay `accepted`; later fulfillment states are not emulated. An
unsupported command fails without provider I/O. `restore_and_reconcile` is
refused; clear the account's faults and run `reconcile_commerce`. Coverage and
intent classification are not location-emulator controls: coverage is the
tenant's `serviceable_pincodes` list. Bind addresses other than loopback
(`127.0.0.1`, `localhost`), `0.0.0.0`, and `::` are rejected.
