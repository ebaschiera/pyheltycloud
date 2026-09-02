# pyheltycloud

Async Python client for the **hcloud** backend of [Helty](https://www.heltyair.com/), used by the
recent control panels of Helty Flow VMC units and by the "Helty Home" app.

Born as the foundation for a `helty_cloud` Home Assistant integration, next to
[`helty`](https://www.home-assistant.io/integrations/helty/) which speaks the local protocol of the
previous panels. The new panels expose no listening port: control necessarily goes through the
manufacturer's cloud.

> **Unofficial.** The API is undocumented: it has been reconstructed by observing the app and
> verified on real hardware. Helty may change it at any time.

## Installation

```bash
pip install pyheltycloud
```

## Usage

```python
import asyncio
from pyheltycloud import HeltyCloud, VmcMode

async def main() -> None:
    async with HeltyCloud("me@example.com", "password") as helty:
        for device in await helty.get_devices():
            print(device.name, device.model)

            # update() wakes the board and then reads: the method to use for polling.
            state = await helty.update(device)
            print(state.mode, state.temperature_indoor, state.humidity)

            # set_mode_verified() re-reads the state and resends if the command is lost.
            await helty.set_mode_verified(device, VmcMode.SPEED_2)

asyncio.run(main())
```

## How it works

Two separate channels. The panel runs no server: it holds an outbound connection to **AWS IoT Core**
(eu-central-1), and Helty's backend relays to it. Clients never reach the panel directly — the app
and this library call the REST API at `api.hcloud.heltyair.com`, authenticating against an **AWS
Cognito** user pool with the account's own email and password (SRP, nothing to provision).

That shape explains both surprises below: a read returns the last message the panel pushed, not the
machine's current state, and a write is a message handed to the relay, so a `200` only means the
backend took it.

## Things to know before using it

**Read, do not poke.** The board reports on its own every 5.5 minutes at the median and every 33
at the 99th percentile, measured over 5334 spontaneous reports, because it reports on any
significant change as well as on its timer. Reading the last message costs one call and the board
never notices, so a poll should use `get_last_telemetry()`. `refresh()` instead wakes the board,
and `update()` wakes it and waits up to ~15 seconds for the answer: keep those for right after a
command, when confirmation is worth the round trip. The vendor asks not to solicit on a short
timer.

**Commands are acknowledged, not guaranteed.** The cloud answers `200` before the board has done
anything. `set_mode()` is a single send; `set_mode_verified()` re-reads the state and retries,
for callers that would rather pay a round trip than miss a command.

**Two different serials.** Reads use `device.serial_number` (the machine), writes use
`device.board_serial` (the board). They are not interchangeable.

## What is covered

| | |
|---|---|
| Reading | devices, mode, indoor and outdoor temperature, humidity, CO2 and VOC where installed, both fan percentages, alarms as named flags, the two counters of treated air |
| Writing | all modes (off, speeds 1-4, night, hyperventilation, free cooling), optionally with verification and retries; report request |
| Not mapped yet | commands that carry an argument: the API refuses every shape tried. The brightness level, the one argument worth having, belongs to the Elite version's lights, so a FlowPLUS may have nothing here |
| Not mapped yet | the filter reset: the vendor gives command 37, unverified, and sending it would destroy the only record of how old the filter is |
| Not available | whether the panel is lit, and whether Auto is on: 40, 41, 38 and 39 take effect, but the vendor states this generation of machines cannot report either state |
| Not available | the unit of the CO2 and VOC readings, undocumented and untestable on a unit that has neither sensor |
| Not available | the alarm threshold of the filter — 90000 m³ on the current firmware, by the vendor's word, and in none of the values the board reports |

## Development

```bash
uv sync --group dev
uv run pytest --cov=pyheltycloud
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy
```

Tests never touch the network: they use a fake local HTTP backend.

## License

MIT.
