# PrintDeck for Home Assistant

The maintained Home Assistant integration for the local
[PrintDeck Unified Printer API](https://printdeck.xyz/unified-printer-api/).
It discovers PrintDeck devices over mDNS, asks for the API token in the Home
Assistant interface and creates devices and entities for every configured
printer. Battery-equipped PrintDeck models also expose battery level and
charging sensors, so automations can react to a low battery. Printers removed
from PrintDeck are removed from Home Assistant after
the next successful refresh. Every printer also exposes diagnostic network
address and network port sensors; its Home Assistant identity remains stable
when the endpoint or display name changes.

[![Open your Home Assistant instance and add this repository to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=PrintDeck&repository=home-assistant&category=integration)

## Install

1. Enable **Unified Printer API** in PrintDeck Web Config and copy its token.
2. Open the HACS button above, or add
   `https://github.com/PrintDeck/home-assistant` as a custom repository of type
   **Integration**.
3. Download **PrintDeck** in HACS and restart Home Assistant.
4. Open **Settings > Devices & services > Add integration > PrintDeck**.
5. Select a discovered PrintDeck, or enter `printdeck.local`, and paste the raw
   API token. Do not add the `Bearer` prefix.

The integration requires PrintDeck firmware that advertises native Home
Assistant support. See the
[complete setup and migration guide](https://printdeck.xyz/unified-printer-api/home-assistant/)
for supported entities and troubleshooting.

## Automation blueprints

- [Turn on a light when a print finishes](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2FPrintDeck%2Fhome-assistant%2Fmain%2Fblueprints%2Fautomation%2Fprintdeck%2Flight_when_print_finishes.yaml)
- [Use different lights at 25%, 50% and 75%](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fraw.githubusercontent.com%2FPrintDeck%2Fhome-assistant%2Fmain%2Fblueprints%2Fautomation%2Fprintdeck%2Fprogress_milestone_lights.yaml)

Polish, Spanish, French, German and Simplified Chinese variants are available
next to the English blueprints.

## Privacy and scope

Communication stays on the local network. Home Assistant polls PrintDeck's
read-only API; printer credentials remain on PrintDeck. Do not expose the API
port to the internet.

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
