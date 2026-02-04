"""
CLI definition.
"""

import json
import urllib.request
import urllib.error
from importlib import import_module as import_

import click

from elva.cli import common_options, file_paths_option_and_argument, pass_config_for
from elva.core import DEFAULT_HOST, DEFAULT_PORT

APP_NAME = "editor"
"""The name of the app."""


def fetch_rooms(host: str, port: int) -> list[str]:
    """
    Fetch list of room identifiers from server.

    Arguments:
        host: the server hostname.
        port: the server port.

    Returns:
        list of room identifiers, or empty list on error.
    """
    url = f"http://{host}:{port}/rooms"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
            rooms = data.get("rooms", [])
            return [r.get("identifier") for r in rooms if r.get("identifier")]
    except (urllib.error.URLError, json.JSONDecodeError):
        return []


def prompt_for_room(host: str, port: int) -> str:
    """
    Prompt user to select or enter a room name.

    Arguments:
        host: the server hostname.
        port: the server port.

    Returns:
        the selected or entered room name.
    """
    rooms = fetch_rooms(host, port)

    if rooms:
        click.echo(f"Available rooms on {host}:{port}:")
        for i, room in enumerate(rooms, 1):
            click.echo(f"  {i}. {room}")
        click.echo()

        choice = click.prompt(
            "Enter room number or new room name",
            default=rooms[0] if rooms else None,
        )

        # Check if user entered a number
        try:
            idx = int(choice)
            if 1 <= idx <= len(rooms):
                return rooms[idx - 1]
        except ValueError:
            pass

        return choice
    else:
        click.echo(f"Could not connect to {host}:{port} to list rooms.")
        click.echo("Specify --host and --port if using a different server.")
        click.echo()
        return click.prompt("Enter room name")


@click.command(name=APP_NAME)
@common_options
@click.option(
    "--ansi-color/--no-ansi-color",
    "ansi_color",
    is_flag=True,
    help="Use the terminal ANSI colors for the Textual colortheme.",
)
@file_paths_option_and_argument
@pass_config_for(APP_NAME)
def cli(
    config: dict,
    *args: tuple,
    **kwargs: dict,
):
    """
    Edit text documents collaboratively in real-time.
    \f

    Arguments:
        config: the merged configuration from CLI parameters and files.
        args: unused positional arguments.
        kwargs: parameters passed from the CLI.
    """
    logging = import_("logging")
    _log = import_("elva.log")
    app = import_("elva.apps.editor.app")

    # Prompt for room if not specified
    if not config.get("identifier"):
        host = config.get("host", DEFAULT_HOST)
        port = config.get("port", DEFAULT_PORT)
        config["identifier"] = prompt_for_room(host, port)

    # logging
    _log.LOGGER_NAME.set(__package__)
    log = logging.getLogger(__package__)

    log_path = config.get("log")
    level_name = config.get("verbose")
    if level_name is not None and log_path is not None:
        handler = logging.FileHandler(log_path)
        handler.setFormatter(_log.DefaultFormatter())
        log.addHandler(handler)

        level = logging.getLevelNamesMapping()[level_name]
        log.setLevel(level)

    # run app
    ui = app.UI(config)
    ui.run()

    # reflect the app's return code
    ctx = click.get_current_context()
    ctx.exit(ui.return_code or 0)


if __name__ == "__main__":
    cli()
