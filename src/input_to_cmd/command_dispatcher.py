import asyncio
from collections.abc import Sequence

from .opc_client import get_running_command as get_running_movement
from .opc_client import move, set_home, shutdown_connection, stop, write_xz_points


class UnsupportedCommandError(ValueError):
    pass


_foreground_commands: list[tuple[object, str]] = []


def get_running_command() -> str | None:
    if _foreground_commands:
        return _foreground_commands[-1][1]
    return get_running_movement()


async def execute_command(
    command: str,
    points: Sequence[tuple[float, float]] | None = None,
    step_size: int | None = None,
) -> None:
    if command in {"move_right", "move_up", "move_left", "move_down"}:
        await move(command, step_size=step_size)
    elif command in {"home", "stop", "point"}:
        if command == "point" and not points:
            raise ValueError("At least one point is required")
        entry = (object(), command)
        _foreground_commands.append(entry)
        try:
            if command == "home":
                await set_home()
            elif command == "stop":
                await stop()
            else:
                assert points is not None
                await write_xz_points(points)
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
