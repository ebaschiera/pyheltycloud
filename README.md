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

**The board rarely speaks.** With the app closed it sends telemetry roughly every half hour, so
`get_state()` may raise `HeltyCloudNoDataError` simply because there is nothing recent. To get
current data the board must be asked to report: `refresh()` does that, and `update()` does it for
you — it waits up to ~15 seconds for the answer and, if none arrives, falls back to the last known
data.

**Commands are acknowledged, not guaranteed.** The cloud answers `200` before the board has done
anything. `set_mode()` is a single send; `set_mode_verified()` re-reads the state and retries,
for callers that would rather pay a round trip than miss a command.

**Two different serials.** Reads use `device.serial_number` (the machine), writes use
`device.board_serial` (the board). They are not interchangeable.

## What is covered

| | |
|---|---|
| Reading | devices, mode, indoor and outdoor temperature, humidity, both fan percentages, alarm bitmap |
| Writing | all modes (off, speeds 1-4, night, hyperventilation, free cooling), optionally with verification and retries; report request |
| Not mapped yet | panel light: the on/off `commandId`s are known, the argument format is not |
| Not available | the filter counter — the firmware counts cubic metres of treated air, and the cloud does not expose it |

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
