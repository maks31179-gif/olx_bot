name: OLX to Discord

on:
  schedule:
    - cron: "*/5 * * * *"
  workflow_dispatch:

permissions:
  contents: write

concurrency:
  group: olx-bot
  cancel-in-progress: false

jobs:
  check:
    runs-on: ubuntu-latest
    timeout-minutes: 8
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Check OLX
        run: python olx_discord.py --once
        env:
          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}
          DISCORD_WEBHOOKS: ${{ secrets.DISCORD_WEBHOOKS }}
          DISCORD_BOT_TOKEN: ${{ secrets.DISCORD_BOT_TOKEN }}

      - name: Save seen
        run: |
          git config user.name "olx-bot"
          git config user.email "olx-bot@users.noreply.github.com"
          for i in 1 2 3 4 5; do
            git fetch -q origin main
            git show origin/main:seen.json > /tmp/remote_seen.json 2>/dev/null || echo '{}' > /tmp/remote_seen.json
            python olx_discord.py --merge-seen /tmp/remote_seen.json
            git reset -q origin/main
            git add seen.json
            git diff --cached --quiet && exit 0
            git commit -q -m "update seen"
            git push -q origin HEAD:main && exit 0
            sleep $((RANDOM % 10 + 3))
          done
          exit 1
