"""DataNodes plugin entry points with an explicit legacy compatibility path."""

from engine.providers.cyberdrop_hosts import DatanodesProvider as LegacyDatanodesProvider
from engine.telemetry import telemetry_bus

from provider import PrimaryFlowUnsupported, resolve as resolve_primary


def _items(operation, params):
    if operation == "refresh":
        item = params.get("item") or {}
        url = item.get("source_url") or params.get("url")
    else:
        url = params.get("url")
    secrets = params.get("secrets")
    try:
        result = resolve_primary(url, secrets)
    except PrimaryFlowUnsupported as exc:
        telemetry_bus.record(
            level="WARN", subsystem="engine:resolve",
            message="[DATANODES_PRIMARY_FALLBACK] Plugin flow was not recognized; using legacy provider",
            context={
                "task_id": (secrets or {}).get("task_id"),
                "operation": operation,
                "reason": str(exc),
            },
            tier="engine",
        )
        result = getattr(LegacyDatanodesProvider, operation)(url, secrets)
    return [item.to_dict() for item in result]


def resolve(params):
    return _items("resolve", params)


def refresh(params):
    item = params.get("item") or {}
    return resolve({
        "url": item.get("source_url") or params.get("url"),
        "secrets": params.get("secrets"),
    })
