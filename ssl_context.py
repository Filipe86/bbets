import ssl
from pathlib import Path


SYSTEM_CA_BUNDLE = Path("/etc/ssl/cert.pem")


def create_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    if SYSTEM_CA_BUNDLE.is_file():
        context.load_verify_locations(cafile=str(SYSTEM_CA_BUNDLE))
    return context
