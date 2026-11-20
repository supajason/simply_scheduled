# Scheduler

A friendly, device-only scheduler for Home Assistant. Version 0.2.0 (experimental).

## What it does

Schedule device entities to turn on and off at specific local times on chosen weekdays, from Home Assistant's sidebar. No YAML is needed when creating schedules.

Supported entity types: switches, lights, climate, fans, covers, humidifiers and input_boolean helpers. Sensors, automations, scripts and scenes are deliberately not supported.

* Light controls: on/off, optional brightness, colour temperature, or RGB colour, based on the entity's advertised capabilities.
* Climate controls: on/off, optional HVAC mode and target temperature; a temperature range for supported heat/cool devices.
* Fan controls: on/off and optional percentage speed on capable fans.
* Cover controls: open/close and optional open percentage on capable covers.
* Humidifier controls: on/off and optional target humidity.
* Switches and input_boolean helpers: on/off only.
* Schedule list groups by device category by default, with a setting to turn grouping off.
* Device selection groups by type by default, with a separate setting to turn grouping off.
* Settings > Visible devices shows a searchable list of entity names and identifiers with checkboxes. Untick a device to hide it from selection without deleting or changing it in Home Assistant. Existing schedules continue to run.
* A bin icon deletes a schedule after confirmation. Schedules can be edited or paused independently.

## Installation and upgrades

This is a Home Assistant OS or Supervised custom add-on **source repository**, not a HACS integration. Home Assistant must be able to build the Docker image and access the internet for Python dependencies.

1. Create a **public GitHub repository** and upload the extracted files preserving the directory structure (`repository.yaml` at the repository root, and `simply_scheduled/config.yaml` inside the app folder).
2. In `repository.yaml`, replace `YOUR_USERNAME` with your account name and adjust the repository URL. The URL can point to your actual repository name if different.
3. Home Assistant: Settings > Add-ons > Add-on Store > (three-dot menu) > Repositories. Add the repository URL.
4. Install **Scheduler**, enable **Start on boot**, start it and select **Open Web UI**. Optionally enable **Show in sidebar**.
5. Use **Settings** inside Scheduler to hide unwanted entities, e.g. `switch.wlan0_allowed_on_network_9`.

For an existing installation from version 0.1.0, keep the same folder `simply_scheduled` and slug `simply_scheduled`, and replace the repository files with this release. Home Assistant can then upgrade the existing add-on instead of installing a duplicate. The displayed name and sidebar title are now **Scheduler**.

## Important migration behaviour

Version 0.1.0 supported scripts, scenes, automations and media players. Version 0.2.0 removes these from scheduling. On first startup with old schedule data, unsupported schedules are removed and the **full original schedule list** is backed up to `/data/schedules-before-device-only-migration.json` before the new file is saved. Existing supported device schedules are kept. Do not overwrite the `/data` folder when upgrading.

Excluding a device from the picker does **not** remove or disable any existing schedules for that device.

## Data and security

* The scheduler runs in the add-on using APScheduler and calls Home Assistant services over the Supervisor Core API, with the Supervisor token held on the backend.
* Ingress authentication is provided by Home Assistant. The panel is configured for Home Assistant administrators only.
* Schedules are stored in `/data/schedules.json`; UI settings are stored in `/data/settings.json` and should be included in add-on backups.
* No unauthenticated external port is published. Do **not** expose port 8099 directly to the internet.
* Only a restricted set of entity domains, on/off actions and typed device options are allowed by the backend.
* Actions are attempted at the schedule time. Missed actions during downtime are not replayed after restart; failures are logged, not automatically retried.

## Known limitations

* Weekly schedules with fixed times only, not sunrise/sunset, intervals, one-off schedules or conditions.
* Available controls depend on what the entity reports through the Home Assistant States API and what its integration implements. A device might expose a capability but reject a service call; check add-on logs if this happens.
* Device visibility is managed at the **entity** level, not entire physical devices. This is deliberate because a physical device may expose multiple switch/light/climate entities; for example, the WLAN control is its own switch entity.
* Currently supports one target device per schedule.
* This is an experimental release. API and JavaScript syntax checks and offline tests are provided, but a real Home Assistant installation is still required for end-to-end testing.
* Schedules use the Home Assistant host timezone (`TZ`). Times that do not exist when the clocks go forward cannot execute.

## Development and tests

Install `simply_scheduled/requirements.txt` in a Python 3.12 environment with internet access. Run:

```bash
python -m unittest discover -s tests -v
SCHEDULES_FILE=./schedules.json SETTINGS_FILE=./settings.json TZ=Europe/London PORT=8099 python simply_scheduled/app/main.py
```

The user interface loads locally without a Home Assistant API, but device discovery and service execution need a live Supervisor environment and token.

## Publishing

This ZIP provides the GitHub repository files, not a directly installable `.addon` binary. Adding the repository to Home Assistant's add-on store is supported; inclusion in any curated public store requires a separate publication process.
