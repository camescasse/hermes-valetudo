# Valetudo Robot Control for Hermes Agent

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that controls a robot vacuum that runs [Valetudo](https://valetudo.cloud). It uses the Valetudo REST API on your local network. It needs no Home Assistant, no MQTT and no cloud account.

This plugin is not affiliated with the Valetudo project.

## Tools

| Tool | Action |
|---|---|
| `valetudo_status` | Read the status, robot error, battery, presets, dock state, consumables, current clean statistics and unread events |
| `valetudo_rooms` | List the rooms (map segments) with their IDs and names |
| `valetudo_clean_rooms` | Clean rooms by name or by segment ID, with 1 or more passes |
| `valetudo_control` | Start a full clean or continue a paused task, stop, pause, return to the dock or locate the robot |
| `valetudo_set_preset` | Set the fan speed, water usage or operation mode, or list the allowed values |
| `valetudo_dock` | Empty the dustbin, start or stop the mop wash, start or stop the mop drying |

Each tool first checks that the robot has the necessary Valetudo capability. A robot without a mop dock, for example, gets a clear error from `valetudo_dock`.

## Limits

The plugin calls a fixed set of endpoints only. It cannot:

- reset or edit the map, or rename rooms
- change no-go zones or virtual walls
- change Wi-Fi, MQTT or other Valetudo settings
- turn off obstacle avoidance or other robot settings

The plugin connects directly to the robot. It ignores proxy settings and does not follow HTTP redirects. It accepts only room names and segment IDs that the robot reports. It refuses presets and pass counts that the robot does not support.

## Install

```sh
hermes plugins install valetudo
hermes plugins enable valetudo
```

The installer asks for `VALETUDO_URL`. Use the address of the Valetudo web interface, for example `http://192.168.1.50`. Do not put a username or password in the URL.

If you turned on HTTP basic auth in Valetudo, add the credentials to `~/.hermes/.env`:

```sh
VALETUDO_USERNAME=<username>
VALETUDO_PASSWORD=<password>
```

In the Hermes Desktop app, you can also set them in Settings → Plugins → Valetudo Robot Control.

Start a new Hermes session. Then ask, for example, "Clean the kitchen and the office twice".

## Requirements

- Hermes Agent 0.22.6 or later
- Valetudo with the v2 REST API. The plugin was tested on Valetudo 2026.10.0.
- Network access from the Hermes host to the robot

The plugin uses only the Python standard library.

## Disclosure

- **Network:** the plugin sends HTTP requests only to `VALETUDO_URL`. It connects directly and ignores `http_proxy` and `https_proxy`. It does not follow redirects. It sends no telemetry.
- **Physical device:** the tools move a robot in your home. Hermes calls them with no confirmation step.
- **Credentials:** the plugin reads `VALETUDO_URL`, `VALETUDO_USERNAME` and `VALETUDO_PASSWORD` through Hermes. It stores nothing.
- **Clear text:** Valetudo serves plain `http://`. Basic auth over `http://` sends the password unencrypted on your network.
- **Model provider:** tool results, such as room names and robot state, go to your model provider.
- **Valetudo without auth:** Valetudo has no login by default. Any device on your network can control the robot. Turn on basic auth in Valetudo to prevent this.

## Development

```sh
python3 -m unittest discover -s tests
hermes plugins validate . --install-deps
```

The tests use only the standard library. They put a stub `agent.secret_scope` module in `sys.modules`, so that they run without a Hermes install. Hermes does not load the `tests` directory.

## License

MIT
