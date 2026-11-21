"""Valetudo plugin for Hermes Agent: control a Valetudo robot vacuum over its local REST API."""

from __future__ import annotations

from . import schemas, tools

TOOLSET = "valetudo"

_TOOLS = (
    (schemas.STATUS, tools.status),
    (schemas.ROOMS, tools.rooms),
    (schemas.CLEAN_ROOMS, tools.clean_rooms),
    (schemas.RENAME_ROOM, tools.rename_room),
    (schemas.CONTROL, tools.control),
    (schemas.SET_PRESET, tools.set_preset),
    (schemas.DOCK, tools.dock),
)


def register(ctx) -> None:
    for schema, handler in _TOOLS:
        ctx.register_tool(
            name=schema["name"],
            toolset=TOOLSET,
            schema=schema,
            handler=handler,
            check_fn=tools.is_configured,
            emoji="🧹",
        )
