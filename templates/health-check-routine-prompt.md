# Health-check routine prompt

Use everything below the line as the instruction of the scheduled routine
`Lark relay health check`. Schedule: every 30 minutes, all day.

---

Check the Feishu/Lark relay and repair it if needed. Stay silent when it is healthy.

1. Run `~/.lark-relay/status.sh`.
2. If the last lines say `status: healthy`, delete `~/.lark-relay/state/health-report.txt` if it
   exists and stop here. Send no message.
3. Otherwise act on each `problem:` line:

   | Problem | Action |
   | --- | --- |
   | `lark_cli_missing` | Run `npm install -g @larksuite/cli`, then `~/.lark-relay/start.sh`. |
   | `listener_down` | Run `~/.lark-relay/start.sh`. |
   | `duplicate_consumer` | Run `~/.lark-relay/stop.sh`, then `~/.lark-relay/start.sh`. |
   | `bot_open_id_missing` | In the skill's repository run `python3 scripts/onboard.py relay --start`. |
   | `relay_crashing`, `never_ready` | Read the log files named on that line and find the cause. Do not restart in a loop. |
   | `webhook_unconfigured`, `forwarding_stalled` | Nothing to repair yourself: the operator must provide or fix the webhook URL and key. |

4. Run `~/.lark-relay/status.sh` again.
5. Tell the operator in one short message only when one of these is true:
   - you restarted or reinstalled something (say what, and whether it is healthy now);
   - a problem remains that you cannot fix (quote its `problem:` line).

   Do not repeat a report: if `~/.lark-relay/state/health-report.txt` already holds the same
   problem lines and today's date, stay silent. Otherwise send the message and write the date
   and the problem lines to that file.

Never print `webhook.env`, a webhook URL, or a key. Never run `supervisor.sh` directly; it does
not return. If the relay lives in another directory, use that directory instead of
`~/.lark-relay`.
