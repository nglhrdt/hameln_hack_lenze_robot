import asyncio

from .opc_client import move, set_home, shutdown_movement, stop


class UnsupportedCommandError(ValueError):
    pass


async def execute_command(command: str) -> None:
    if command == "home":
        await set_home()
    elif command in {"move_right", "move_up", "move_left", "move_down"}:
        await move(command)
    elif command == "stop":
        await stop()
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
        await shutdown_movement()


if __name__ == "__main__":
    asyncio.run(main())
