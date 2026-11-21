"""Tool schemas that the model reads."""

STATUS = {
    "name": "valetudo_status",
    "description": (
        "Get the current state of the Valetudo robot vacuum: status (docked, cleaning, "
        "returning, error and so on), battery level, fan speed, water usage and operation "
        "mode presets, dock status, attachments, dock components, consumables, statistics "
        "of the current or last clean, and unread robot events such as errors. "
        "Use this before you control the robot and to answer questions about it."
    ),
    "parameters": {"type": "object", "properties": {}},
}

ROOMS = {
    "name": "valetudo_rooms",
    "description": (
        "List the rooms (map segments) of the Valetudo robot vacuum, with their IDs and "
        "names, and the allowed number of cleaning passes. A room without a name has name null. "
        "Use this to find the room names or IDs before you call valetudo_clean_rooms."
    ),
    "parameters": {"type": "object", "properties": {}},
}

CLEAN_ROOMS = {
    "name": "valetudo_clean_rooms",
    "description": (
        "Start a clean of one or more rooms with the Valetudo robot vacuum. Give room names in "
        "rooms (case does not matter), segment IDs in segment_ids, or both. A room without a "
        "name is available only by its segment ID. Get the names and IDs from valetudo_rooms. "
        "The robot leaves the dock and starts at once, and it selects the order of the rooms. "
        "The current fan speed, water usage and operation mode presets apply."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "rooms": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Room names, for example [\"Kitchen\", \"Office\"]",
            },
            "segment_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Segment IDs from valetudo_rooms, for example [\"10\"]",
            },
            "iterations": {
                "type": "integer",
                "minimum": 1,
                "description": "Number of cleaning passes for each room. Default 1. valetudo_rooms gives the maximum.",
            },
        },
    },
}

CONTROL = {
    "name": "valetudo_control",
    "description": (
        "Send a basic command to the Valetudo robot vacuum. "
        "start: clean the full home, or continue a paused task. stop: stop the current task. "
        "pause: pause the current task. "
        "home: return to the dock. locate: play a sound so that a person can find the robot."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["start", "stop", "pause", "home", "locate"],
            },
        },
        "required": ["action"],
    },
}

SET_PRESET = {
    "name": "valetudo_set_preset",
    "description": (
        "Set a preset on the Valetudo robot vacuum. fan_speed sets the suction power, "
        "water_usage sets the mop water level, operation_mode selects vacuum, mop or both. "
        "The allowed values depend on the robot. Call without value to get the allowed values."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "preset": {
                "type": "string",
                "enum": ["fan_speed", "water_usage", "operation_mode"],
            },
            "value": {
                "type": "string",
                "description": "Preset value, for example \"high\" or \"vacuum_and_mop\". Omit it to list the allowed values.",
            },
        },
        "required": ["preset"],
    },
}

DOCK = {
    "name": "valetudo_dock",
    "description": (
        "Start or stop a dock action on the Valetudo robot vacuum. The robot must be in its dock. "
        "empty_dustbin: empty the robot dustbin into the dock bag. "
        "wash_mop / stop_mop_wash: start or stop the mop wash. "
        "dry_mop / stop_mop_dry: start or stop the mop drying."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["empty_dustbin", "wash_mop", "stop_mop_wash", "dry_mop", "stop_mop_dry"],
            },
        },
        "required": ["action"],
    },
}
