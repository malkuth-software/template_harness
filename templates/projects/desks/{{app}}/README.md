# {{title}} desk

The coordination files for {{title}} (`{{repository}}`):

| File | What |
|---|---|
| `INTEGRATION_QUEUE.md` | coder handoffs waiting for the integrator, when SushiComms is not used |
| `sushicomms.json` | SushiComms' ticket store (the app's `.sushi/sushicomms.json` points here); written by the plugin, committed by the orchestrator |

Seats: `../seats/{{app}}-orchestrator.md`, `{{app}}-w1.md`, `{{app}}-integrator.md`.
The role registry is the app's `.sushi/sushicomms/roles.json`.
