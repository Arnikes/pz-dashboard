"""An independently imported extension used by the integration contract tests."""

from extensions import ActionSpec, ChannelSpec, Plugin


def build_plugin(completed):
    """Compose an action and its read-only status channel without changing the host."""

    def prepare(data, context):
        value = data.get("value", "example")
        return lambda: completed.append((value, context.warn_seconds))

    def collect():
        return {"ok": True, "completed": len(completed), "message": "Сервер остановлен"}

    return Plugin(
        "example",
        actions=(ActionSpec("example-probe", prepare),),
        channels=(
            ChannelSpec(
                "example-status", collect, interval=30, route="/api/plugins/example/status"
            ),
        ),
    )
