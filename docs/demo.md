# Reproducible demonstration

Use the fictional orders task in [examples/task.md](../examples/task.md). Keep the original terminal recording, commands, tool versions and report. A GIF or edited screencast is presentation material; it is not a substitute for the underlying evidence.

## README screenshot

[The README screenshot](assets/agentcrew-tui.png) is a capture of the actual Textual app, populated with fictional SQLite records. It illustrates two completed reviews, passing checks and a P1 hypothesis that blocks acceptance. The candidate hash and findings are demonstration data, not evidence of a model run. No agents or providers are started.

From the project checkout with its virtual environment active:

```sh
env -u NO_COLOR python scripts/capture_readme.py
uvx --from cairosvg cairosvg docs/assets/agentcrew-tui.svg \
  -o docs/assets/agentcrew-tui.png -s 1.5
```

The first command uses the installed app and saves an [SVG screenshot](assets/agentcrew-tui.svg). The second converts it to PNG with the optional CairoSVG tool; it requires [uv](https://docs.astral.sh/uv/getting-started/installation/) and a system Cairo library. Neither tool is an AgentCrew runtime dependency. The [capture script](../scripts/capture_readme.py) keeps the sample data reproducible and separate from production state.

## No-model infrastructure clip

[Recorded terminal output](demo.cast) is an asciinema v2 recording of the installed wheel's actual CLI on Linux: version, doctor, empty status and Russian help. **No model call.** It proves this infrastructure path, not generated code quality. Play it with `asciinema play docs/demo.cast` after installing the optional recorder/player.

Activate the installed virtual environment, then record an ordinary terminal using `asciinema rec docs/demo.cast` if asciinema is installed. Inside it, run:

```sh
agentcrew --version
agentcrew doctor --json
agentcrew status --json
agentcrew --lang ru --help
```

Exit the recorder after the commands finish. This clip shows real infrastructure and current state. Label it explicitly **no model call**. The recording tool is optional and is not a runtime dependency. Do not show fabricated agent output or imply that a doctor clip proves CSV development.

## Full task clip after paid-call consent

```sh
AGENTCREW_DEMO_DIR="$(mktemp -d)"
cp examples/task.md "$AGENTCREW_DEMO_DIR/task.md"
agentcrew new --directory "$AGENTCREW_DEMO_DIR/orders" \
  --template python-cli --spec "$AGENTCREW_DEMO_DIR/task.md" --provider codex
agentcrew plan RUN_ID --format md
agentcrew report RUN_ID --format md
agentcrew diff RUN_ID
agentcrew apply RUN_ID --dry-run
agentcrew apply RUN_ID
cd "$AGENTCREW_DEMO_DIR/orders"
python orders_cli.py status
python orders_cli.py export-orders --output orders.csv
cat orders.csv
```

Use the actual displayed run ID. Show the baseline's missing export, the candidate SHA, exact requirement checks, both reviewer results, unchanged source before apply and the applied behavior afterwards. Show any failed criterion honestly. Then record review of a known planted defect and the separate `fix` child run. Repeat with the TypeScript template and its native Node CLI for acceptance evidence.

Do not publish raw traces, private paths or credentials. Inspect the cast before sharing; sanitize a copy while retaining the original privately. A public GIF/screencast of the full real-provider workflow is **pending**. If a no-model clip is present, it is only infrastructure evidence.
