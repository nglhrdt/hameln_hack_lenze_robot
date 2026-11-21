import asyncio

from .opc_client import get_running_command as get_running_movement
from .opc_client import move, set_home, shutdown_connection, stop


class UnsupportedCommandError(ValueError):
    pass


_foreground_commands: list[tuple[object, str]] = []


def get_running_command() -> str | None:
    if _foreground_commands:
        return _foreground_commands[-1][1]
    return get_running_movement()


async def execute_command(command: str) -> None:
    if command in {"move_right", "move_up", "move_left", "move_down"}:
        await move(command)
    elif command in {"home", "stop"}:
        entry = (object(), command)
        _foreground_commands.append(entry)
        try:
            if command == "home":
                await set_home()
            else:
                await stop()
        finally:
            _foreground_commands.remove(entry)
    else:
        raise UnsupportedCommandError(f"Command is not supported: {command}")


async def main() -> None:
    try:
        while True:
            try:
                command = (await asyncio.to_thread(input, "Enter command (or quit): ")).strip()
            except EOFError:
                break
            if command == "quit":
                break
            await execute_command(command)
    finally:
        await shutdown_connection()


if __name__ == "__main__":
    asyncio.run(main())
