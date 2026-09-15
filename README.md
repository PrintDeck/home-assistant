# PrintDeck for Home Assistant

The maintained Home Assistant integration for the local
[PrintDeck Unified Printer API](https://printdeck.xyz/unified-printer-api/).
It discovers PrintDeck devices over mDNS and offers a choice of HTTP API or
MQTT through the broker configured in Home Assistant. Both transports create
the same PrintDeck devices and entities for every configured printer. Battery-equipped PrintDeck models also expose battery level and
charging sensors, so automations can react to a low battery. Printers removed
from PrintDeck are removed from Home Assistant after
the next successful refresh. Every printer also exposes diagnostic network
address and network port sensors; its Home Assistant identity remains stable
when the endpoint or display name changes. Network diagnostic values come
directly from the API's `printer.network` object. The integration displays its
address text as supplied and does not interpret printer protocols or infer ports.
Older firmware without this optional object leaves those two diagnostic values
unknown while printer status continues to work. Home Assistant communicates
only with PrintDeck and does not follow API redirects to another address.

[![Open your Home Assistant instance and add this repository to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=PrintDeck&repository=home-assistant&category=integration)

## Install

1. Choose the connection described below and configure it in PrintDeck Web Config.
2. Open the HACS button above, or add
   `https://github.com/PrintDeck/home-assistant` as a custom repository of type
   **Integration**.
3. Download **PrintDeck** in HACS and restart Home Assistant.
4. Open **Settings > Devices & services > Add integration > PrintDeck**.
5. Select **HTTP API** or **MQTT via Mosquitto** and follow the steps below.

The integration requires PrintDeck firmware that advertises native Home
Assistant support. See the
[complete setup and migration guide](https://printdeck.xyz/unified-printer-api/home-assistant/)
for supported entities and troubleshooting.

## Choose a connection

| Mode | Home Assistant setup | Entity integration |
| --- | --- | --- |
| HTTP API through HACS | Install PrintDeck; enter the local address and Unified API token | PrintDeck |
| MQTT through HACS | Install PrintDeck; configure HA MQTT and enter the PrintDeck topic root | PrintDeck |
| Automatic MQTT Discovery | Configure HA MQTT and enable automatic Discovery in PrintDeck; HACS is optional | MQTT |

For **HTTP API**, enable Unified Printer API in PrintDeck Web Config, choose
HTTP API when adding the integration, and enter its local address and raw token
without the `Bearer` prefix. Existing installations continue to use HTTP.

For **MQTT through HACS**:

1. Install Mosquitto or use an existing local MQTT broker. Configure Home
   Assistant's built-in MQTT integration to connect to it.
2. Enable MQTT in PrintDeck Web Config and connect it to the same broker.
   Broker credentials are separate from the Unified API token. Home Assistant
   keeps its own broker credentials; the PrintDeck integration does not store
   or request another copy.
3. Keep **automatic Home Assistant Discovery** in PrintDeck disabled. If it was
   enabled, wait for cleanup to finish and the standard MQTT entities to disappear
   from Home Assistant before proceeding.
4. Open the discovered PrintDeck in Home Assistant and choose **MQTT via
   Mosquitto**. The integration determines its topic automatically and verifies
   the device through the broker. No topic entry is needed. If the connection is
   not ready, follow the displayed steps and submit again to retry.

When adding a device manually without discovery, copy **MQTT topic root** from
PrintDeck Web Config → Device Settings → Unified API → MQTT into the setup form.
Use the complete value for your device, for example
`printdeck/printdeck-a1b2c3d4e5f6/v1`, not the broker address. Reconfiguring an
existing HTTP integration to MQTT also determines the topic automatically from
the saved device identity.

For **automatic MQTT Discovery without HACS**, complete the first two MQTT steps
and enable automatic Home Assistant Discovery in PrintDeck. Its devices and
entities appear under Home Assistant's built-in MQTT integration. Do not add a
second PrintDeck HACS integration for the same device. Both paths support the
blueprints below; select the sensors belonging to the chosen path.

### Change an existing connection

Use **Reconfigure** on the existing PrintDeck integration to switch between
HTTP and MQTT. Choose the same physical PrintDeck: the stable device and printer
identifiers remain unchanged, so existing entities and automations remain attached.
Switching to MQTT replaces the stored HTTP token and host with the MQTT topic root;
switching back removes that topic root. The old transport is unloaded before the
new one is set up.

Standard MQTT Discovery and the HACS integration belong to different HA platforms.
Moving between them does not automatically migrate entity IDs or automations.
Choose one path per PrintDeck. Before moving from standard Discovery to HACS,
disable automatic Discovery in PrintDeck, leave the old broker reachable until
cleanup finishes, and wait until HA removes its MQTT entities. If the old broker
is no longer reachable, remove its retained PrintDeck discovery entries and the
corresponding obsolete HA MQTT entities manually. Before moving from HACS to
standard Discovery, disable or remove the PrintDeck HACS entry first.

Both HACS transports check HA's remaining standard MQTT entities before setup
and during operation. The MQTT transport also checks the firmware's discovery
and cleanup flags. If a conflict appears later, PrintDeck entities become
unavailable and a repair notice explains how to resolve it. Disable or remove
the HACS entry before deliberately moving to automatic Discovery.

### MQTT state and recovery

The MQTT transport uses Home Assistant's broker connection without opening direct
printer sessions. It subscribes only to the selected PrintDeck namespace:

- `/info`: retained device identity and discovery ownership.
- `/printers`: retained complete profile catalog.
- `/device`: live power state in `{ "api_version": "v1", "device": { "power": {} } }`.
- `/printers/<id>/status`: live status in `{ "api_version": "v1", "status": {} }`.
- `/availability`: `online` or `offline` with the device's last will.

Firmware also publishes `/printers/<id>/nozzles` and `/materials` for other local
consumers; the current HACS entity set uses the same status and power fields as
HTTP. Telemetry is not retained. The integration waits for fresh power and status
messages for the complete catalog, expires them after 90 seconds, and discards
live state after an offline event or broker connection change. It retries initial
setup when the device or broker is unavailable. No incomplete or malformed update
removes printer devices. Up to ten profiles and 64 KiB per message are accepted;
unknown topics and unconfigured printer IDs do not grow the cache. Catalog,
power and status wrappers carry `_mqtt_generation`, an eight-digit hexadecimal
boot/session nonce and eight-digit hexadecimal catalog counter separated by `-`.
A changed catalog generation discards cached live state; only matching fresh
status and power messages can restore entities. This prevents a retained catalog
from being combined with telemetry for a different profile revision.

Only the printer selected in PrintDeck has a full live connection. Other printers
keep their existing summary polling cadence. Missing values remain unknown;
`stale` and `summary` semantics are preserved in both transports.

## Print events

Firmware with print-event support adds a **Print events** entity per printer
and a **Printer condition** sensor. Existing sensors and their unique IDs stay
unchanged. Known progress and elapsed time can also be supplied by summary
polling; they are not restricted to the selected printer.

The integration emits `printdeck_event` on Home Assistant's event bus. Its
payload includes `event_type`, `printdeck_id`, `printer_id`, `printer_name`,
`device_id` (the HA printer device), `job_id`, `event_id`, `routing_key`,
`job_kind`, `progress_percent`, `milestone` and `condition`.
The routing key is `<printdeck_id>:<printer_id>`. Several PrintDecks observing
the same printer intentionally remain independent sources.

Use the **Respond to print events** blueprint in
`blueprints/automation/printdeck/print_events.yaml` for all current and future
printers. Choose actions and event types; leave the source empty for every
PrintDeck, or enter a `printdeck_id` to select one source. Polish, Spanish,
French, German and Simplified Chinese variants are alongside it. Action
variables include `printer_name`, `event_type`, `milestone` and the source IDs.
This blueprint requires HACS; native MQTT Discovery event entities can be used
directly in Home Assistant automations.

Supported types are `started`, `paused`, `resumed`, `completed`, `failed`,
`cancelled`, `milestone`, `attention` and `attention_cleared`. `started` includes
job preparation. Progress thresholds are 25, 50 and 75; a jump across multiple
thresholds emits each new threshold. The actual percentage is separate from
`milestone`. The blueprint excludes calibration jobs by default.

PrintDeck generates its own local job IDs without relying on printer-specific
identifiers. An ID describes an observed session, not a permanent print-history
record. Initial connection and recovery are quiet. A print already running at
startup does not announce a new start. A device restart or an observation gap
may establish a new local session. Event history is limited to eight records
per printer in RAM. HACS ignores records older than 30 seconds and does not
replay them after reconnecting. Intermediate states between samples can be
missed; this is not guaranteed delivery. Older firmware retains its normal
sensors and leaves Print events unavailable.

For native MQTT Discovery, firmware publishes individual events without retain
at `printdeck/<printdeck_id>/v1/printers/<printer_id>/events`. The source IDs,
routing key and event ID are included in each message. The existing rule of
choosing either HACS or standard MQTT Discovery for each PrintDeck still applies.

## Automation blueprints

- [Turn on a light when a print finishes](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2FPrintDeck%2Fhome-assistant%2Fmain%2Fblueprints%2Fautomation%2Fprintdeck%2Flight_when_print_finishes.yaml)
- [Use different lights at 25%, 50% and 75%](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2FPrintDeck%2Fhome-assistant%2Fmain%2Fblueprints%2Fautomation%2Fprintdeck%2Fprogress_milestone_lights.yaml)

Polish, Spanish, French, German and Simplified Chinese variants are available
next to the English blueprints.

## Privacy and scope

Communication stays on the local network. Home Assistant reads PrintDeck through
HTTP or the configured MQTT broker; printer credentials remain on PrintDeck.
Both paths are read-only. Keep the API and broker local, use broker authentication
and validate certificates when using TLS. Grant MQTT read access only to trusted
local consumers: telemetry can include printer names, job names and network metadata.

This repository contains the Home Assistant integration, its automation
blueprints, branding and tests. Learn more about PrintDeck, supported hardware
and firmware installation at [printdeck.xyz](https://printdeck.xyz/).

## Development

Run the focused client tests with:

```sh
python3 -m unittest discover -s tests -v
```

HACS and Home Assistant `hassfest` validation also run on every change.

## License

This Home Assistant integration is licensed under the
[Apache License 2.0](LICENSE). The separate PrintDeck product repository retains
its own license.
