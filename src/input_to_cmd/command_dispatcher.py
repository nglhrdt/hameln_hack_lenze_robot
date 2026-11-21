from .opc_client import set_home


class UnsupportedCommandError(ValueError):
    pass


async def execute_command(command: str) -> None:
    if command == "home":
        await set_home()
    else:
        raise UnsupportedCommandError(f"Command is not supported: {command}")
