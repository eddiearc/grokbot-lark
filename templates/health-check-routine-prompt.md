# Health-check routine prompt

Run this routine every 30 minutes, all day. The Feishu/Lark bot can receive
messages at any time.

## What to do

1. Run `~/.lark-relay/status.sh` (adjust the path if the relay lives elsewhere).
2. If the supervisor, `relay.py`, and the single `event consume` process are up,
   and there is no forwarding error that needs attention, **stay silent**. Do
   not message the operator when everything is healthy.
3. If the listener is down, start it again with `~/.lark-relay/supervisor.sh`
   (only one supervisor; it holds a lock so a second consumer will not start).
   Then re-run `status.sh`.
4. Message the operator only when:
   - you had to restart the listener,
   - a restart failed,
   - or forwarding is failing (for example webhook credentials are missing and
     the spool is growing, or logs show repeated webhook/post errors).
5. Never print `webhook.env`, webhook URLs, or bearer keys. `status.sh` already
   redacts them.
