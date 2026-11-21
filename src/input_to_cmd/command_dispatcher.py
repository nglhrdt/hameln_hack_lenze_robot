import asyncio

from .opc_client import move, set_home, stop


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
    command = input("Enter command: ").strip()
    await execute_command(command)


if __name__ == "__main__":
    asyncio.run(main())
