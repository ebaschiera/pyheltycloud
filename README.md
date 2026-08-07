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

## Things to know before using it

**The board rarely speaks.** With the app closed it sends telemetry roughly every half hour, so
`get_state()` may raise `HeltyCloudNoDataError` simply because there is nothing recent. To get
current data the board must be asked to report: `refresh()` does that, and `update()` does it for
you — it waits up to ~15 seconds for the answer and, if none arrives, falls back to the last known
data.

**Commands can get lost.** The cloud accepts the request and answers `200` without guaranteeing
delivery: in testing roughly one send out of four had no effect. `set_mode()` is a single send;
`set_mode_verified()` re-reads the state and retries until the mode is applied.

**Two different serials.** Reads use `device.serial_number` (the machine), writes use
`device.board_serial` (the board). They are not interchangeable.

## What is covered

| | |
|---|---|
| Reading | devices, mode, indoor and outdoor temperature, humidity, both fan percentages, alarm bitmap |
| Writing | all modes — off, speeds 1-4, night, hyperventilation, free cooling — (also with verification and retries), report request |
| Not mapped yet | panel light (on/off `commandId`s observed but argument format unverified), filter hours |

## Development

```bash
pip install -e ".[dev]"
pytest --cov=pyheltycloud
ruff check src tests && ruff format --check src tests
mypy
```

Tests never touch the network: they use a fake local HTTP backend.

## License

MIT.
