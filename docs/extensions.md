# Modules and trusted extensions

PZ Console can load explicitly registered Python worker actions and monitoring
channels. Extensions run in the panel process with its filesystem and network
access. Only install trusted code. The host owns authentication, request
validation, operation admission, localization, and SSE delivery.

Run a separate panel process for each managed server. Operation state and
settings are process-wide.

## Compose an extension

Create a deployment entrypoint beside `dashboard/app.py`. This example adds
an action and a read-only channel.

```python
import app
from extensions import ActionSpec, ChannelSpec, Plugin

completed = []


def prepare_probe(data, context):
    def work():
        completed.append(context.warn_seconds)

    return work


def collect_probe():
    return {"ok": True, "completed": len(completed)}


plugin = Plugin(
    "example",
    actions=(ActionSpec("example-probe", prepare_probe),),
    channels=(
        ChannelSpec(
            "example-status",
            collect_probe,
            interval=10,
            route="/api/plugins/example/status",
        ),
    ),
)

if __name__ == "__main__":
    app.main(plugins=(plugin,))
```

Run this entrypoint with the normal panel environment and administrator
configuration. The authenticated `GET /api/plugins/example/status` returns the
collector result, and `GET /api/stream` delivers `example-status` events. Start
the action with an authenticated `POST /api/action` and
`{"op":"example-probe"}`. See [API authentication and operation results](operations.md#api).

The standard `app.main()` runs the built-ins. Embedded callers can pass
`plugins` or an explicitly composed `registry` to `ThreadingHTTPServer`.
Attach the ordinary auth manager before serving requests. A registry is immutable.
Duplicate registrations and attempts to replace reserved actions, events, or
routes fail during composition. Extensions supply backend capabilities.

## Worker actions

`ActionSpec(name, prepare, before_start=None)` registers an action.
`prepare(data, context)` validates input without side effects and returns a
zero-argument worker. The context contains a detached settings snapshot and
normalized `warn_seconds`.

`before_start(data, context)` runs under the mutation gate just before worker
admission. Keep it short. Put network calls and filesystem work in the worker.
Return `None` on success or `"cancelled"` / `"aborted"` for cancellation.

Raise `actions.ActionError(status, message)` for an intentional API rejection
or `ops.OpsError(message)` for an expected operational failure. Messages
are visible to administrators and must omit secrets. The host reports unexpected
exceptions as a generic localized failure and logs the exception type.

## Monitoring channels

`ChannelSpec(name, collect, interval=5.0, route=None, telemetry=False)`
registers an SSE event and optional polling endpoint. The interval must be
finite and positive. Place custom routes under `/api/plugins/<plugin-name>/`
without query strings or fragments.

Return a locale-neutral JSON object. Use `{"ok": false, "error": "..."}` for
expected collector failures. Unexpected errors and invalid output become a
generic localized error payload in polling and streaming.

Polling collects current data. Streaming shares one snapshot per channel
and server, then encodes frames by language. Use `telemetry=True` only for
monitoring collectors that accept the `state_provider` keyword for streaming
and also work without it for polling. Lifecycle and mutation checks require
live state.
