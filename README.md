# Home Assistant Apps

Custom Apps (add-ons) for Home Assistant.

## Installation

1. In Home Assistant: **Settings → Apps → App store → ⋮ → Repositories**.
2. Add `https://github.com/brainstormi/home-assistant-apps`.
3. The Apps in this repository show up in the store.

Apps from the same repository share the same id prefix, so they can find each other without configuration.

## Apps

| App | Description |
|-----|-------------|
| [Alarm Contact Center](alarm_contact_center/) | Alerts a list of contacts when the alarm goes off: Asterisk phone calls, Home Assistant app notifications, email and Telegram. |

## Development

Each App lives in its own folder with its code, its tests and its `requirements.txt`. To run an App's tests:

```sh
cd <app_folder>
python -m unittest discover -s tests -t . -v
```

The CI workflow (`.github/workflows/tests.yaml`) runs tests, the add-on linter and the image build for every App listed in its matrix.
