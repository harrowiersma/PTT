from fastapi import Request

from server.murmur.client import MurmurClient


def get_murmur_client(request: Request) -> MurmurClient | None:
    return getattr(request.app.state, "murmur_client", None)
