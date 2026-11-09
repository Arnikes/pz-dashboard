# Modules and trusted extensions

The console supports explicit composition of trusted Python extensions for worker
actions and monitoring channels. The host continues to own authentication,
same-origin request checks, operation admission, request reconciliation,
localization, and SSE delivery.

Extensions execute in the console process with its filesystem and network access.
Installing an extension is a deployment change made by an administrator. There is
no filesystem discovery, HTTP installation endpoint, hot reload, or code sandbox.

## Architecture audit

The audit identified these boundaries, ordered by the value and risk of extraction:

| Boundary | Previous coupling | Result or next step |
| --- | --- | --- |
| Worker action registration | `actions.dispatch` maintained a fixed worker dictionary. | Action specifications let another Python module contribute an action while retaining the host operation slot. |
| Monitoring registration | Providers, polling paths, SSE intervals, and telemetry opt-ins lived in parallel tables. | Channel specifications describe a collector once for polling and streaming. |
| Application composition | The HTTP handler used process-global registration tables. | Each HTTP server receives an immutable extension composition and its own stream cache. |
| Backup and restore | `ops.py` combines storage, archive validation, restore orchestration, and retention. | Extract storage and archive mechanisms before moving lifecycle coordination. |
| Scheduled work | The scheduler directly coordinates editor verification, image checks, Workshop checks, and backups. | Introduce explicit task inputs, ordering, and error policy before separating scheduler tasks. |
| Watchdog | Live probes, restart grace periods, and restart decisions share operation state in `ops.py`. | Separate probe and policy inputs while retaining live checks and the central admission gate. |
| Settings persistence | Revision tracking, masking, raw notification credentials, and schedule writes share the settings owner in `ops.py`. | Define a versioned settings store with explicit public and private reads before adding configurable providers. |
| Event recording | Event persistence, bounded history, process logging, and notification enqueueing meet in `ops.log_event`. | Extract the journal mechanism while preserving event ordering, bounded reads, and nonblocking notification delivery. |
| Notification delivery | The queue and delivery logic depend on Telegram and raw settings from `ops`. | Introduce a delivery sink after defining filtering, bounded queues, and safe errors. |
| Configuration transactions | The editor coordinates revision checks, two Workshop stages, file publication, startup, and recovery. | Preserve a single transaction owner until phase inputs and rollback invariants are explicit. |
| Frontend features | Navigation, polling/SSE subscriptions, action controls, and rendering remain coordinated by `app.js` and `editor.js`. | Extract view modules and explicit subscription lifetimes before designing frontend plugin registration; backend extensions currently add no UI. |

The first three boundaries are implemented here. A generic event bus, router,
settings framework, or frontend plugin system would add policy without resolving
the remaining transaction and lifecycle dependencies.

Per-server registration and caching do not provide separate managed servers.
Operation state, the worker slot, settings, configuration, and several domain
caches remain process-global. Use separate processes to manage independent PZ
instances.

## Compatibility boundaries

Built-in operation names, HTTP routes, response fields, polling behavior, and SSE
events retain their contracts. Cancellation and request resolution remain control
actions. Configuration preparation and application retain their editor preflight
and recovery policy. Extensions contribute ordinary background worker actions;
they cannot replace these reserved controls.

## Compose an extension

The public contracts live in `dashboard/extensions.py`; `runtime.build_registry`
adds the built-in actions and channels. Import extension modules explicitly in a
deployment entrypoint. The normal `app.main()` continues to run the built-in
console without extensions.

For example, place the following in `dashboard/example_extension.py`:

```python
from extensions import ActionSpec, ChannelSpec, Plugin

completed = []


def prepare_probe(data, context):
    # Preparation validates and binds arguments; it must not mutate server state.
    def work():
        completed.append(context.warn_seconds)

    return work


def collect_probe():
    # Return locale-neutral data; the host translates display fields for EN/RU.
    return {"ok": True, "completed": len(completed), "message": "Готово"}


PLUGIN = Plugin(
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
```

Create `dashboard/run_with_extensions.py` beside it:

```python
import app
from example_extension import PLUGIN

if __name__ == "__main__":
    app.main(plugins=(PLUGIN,))
```

Run this entrypoint with the same environment and administrator configuration as
the normal console. The authenticated `GET /api/plugins/example/status` returns
the collector result. `GET /api/stream` includes an `example-status` event every
10 seconds. Start the action with the existing authenticated, same-origin
`POST /api/action` request and `{"op": "example-probe"}`. Action receipts and
request reconciliation use the same fields as built-in operations. This registers
backend capabilities; it does not add navigation or buttons to the frontend.

Tests and embedded callers can construct a server directly:

```python
import app
from example_extension import PLUGIN

server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler, plugins=(PLUGIN,))
# Attach the ordinary auth manager before serving requests.
```

Alternatively, pass an explicitly composed `registry` to the server constructor.
Do not pass both `plugins` and `registry`. A registry is immutable after creation;
it copies caller-owned collections so subsequent list mutations cannot change a
running server. Duplicate plugin, action, channel, or polling route registrations
fail during composition rather than silently replacing another capability.

### Worker actions

`ActionSpec(name, prepare, before_start=None)` contributes an ordinary background
operation. `prepare(data, context)` returns a zero-argument callable. The frozen
`ActionContext` contains a detached settings snapshot and normalized
`warn_seconds`; it does not expose an HTTP handler. Treat nested settings values
as a snapshot, not as persistent storage.

Preparation must be side-effect-free. The host owns the operation request scope,
checks busy state, and enters the atomic mutation gate. The optional
`before_start(data, context)` runs inside that gate immediately before the worker
is admitted. Keep it short: it is intended for admission-time bookkeeping, not
network calls, filesystem workflows, or background work. The worker then runs
through the same serialized `ops.start_op` lifecycle as built-in actions.

Raise `actions.ActionError(status, message)` for an intentional API rejection or
`ops.OpsError(message)` for an expected operational conflict/failure. These
messages are authored user-facing diagnostics and must not contain secrets. An
unexpected exception from extension preparation, admission hooks, or worker
execution becomes a generic localized failure. The host logs its exception type
without the exception message, releases admission/request tracking, and permits
later actions. Existing operation names and control/editor actions are reserved.

An extension worker returns `None` on success or `"cancelled"`/`"aborted"` to
record a cancellation. Other return values fail the operation. The host-only
dashboard-update `"handoff"` result cannot be requested by an extension.

### Monitoring channels

`ChannelSpec(name, collect, interval=5.0, route=None, telemetry=False)` contributes
an SSE event and optionally a polling route. Intervals must be finite positive
numbers; booleans are rejected. A custom route must be below
`/api/plugins/<plugin-name>/` without query strings, fragments, or path traversal.
SSE control event names, including `auth-expired`, are reserved.

Collectors return JSON-compatible data. Expected failures should be returned as
an authored `{"ok": false, "error": "..."}` payload. Unexpected collector errors,
invalid JSON values, and invalid Unicode produce the same generic localized
`ok: false` payload for polling and streaming; polling retains HTTP 200 like the
built-in monitoring endpoints. Arbitrary exception messages are not published.

With `telemetry=True`, the SSE collector receives
`state_provider=cache.container_state`; direct polling still calls `collect()`
without a cached state provider. Only opt in when the collector accepts that
keyword argument. The opt-in is for monitoring and must not be used for lifecycle
waits or mutations. Channel collection is shared among concurrent subscribers;
a failed SSE snapshot is cached for its interval and retried after expiry.

Polling always collects fresh data. SSE shares a short-lived, locale-neutral
snapshot per channel and per HTTP server, then encodes separate English and
Russian frames. Collectors must return locale-neutral data rather than translating
their response before caching it. User-visible display fields use the host's
English/Russian presentation catalog; opaque values remain unchanged.

## Further extraction

Extract a domain when a real new implementation can depend on a stable contract.
Keep the following invariants with their current owners until those contracts are
defined and tested:

- Lifecycle waits, watchdog probes, mutations, and restore decisions must use live
  state. The telemetry snapshot must not become a lifecycle cache.
- Backup storage adapters must preserve filename validation, bounded reads,
  retention semantics, and failure recovery.
- Scheduled tasks must preserve persisted next-run state and the atomic worker
  admission gate, including retry after a race.
- Settings storage must preserve revision conflicts, detached snapshots, secret
  masking for public reads, and restricted raw credential access for delivery.
- Event journals must preserve bounded tail reads and event order. Slow or failed
  subscribers must not delay recording or create notification error loops.
- Notification sinks must not block event recording and must mask credentials in
  diagnostics.
- Configuration phases must preserve revisions, secret masking, durable writes,
  and rollback journals when recovery is incomplete.
- Frontend view modules must preserve operation reconciliation, unsaved drafts,
  localization, focus, and subscription cleanup before accepting custom views.

See [server operations](operations.md), the [DRY and KISS audit](dry-kiss-audit.md),
and the [verification workflow](../CONTRIBUTING.md).
