from .generic import GenericProvider
from .gofile import GofileProvider
from .mega import MegaProvider
from .transferit import TransferItProvider
from .hosted import (MediaFireProvider, GoogleDriveProvider, PixeldrainProvider,
                     OneFichierProvider, KrakenfilesProvider, CyberdropProvider)
from .cyberdrop_hosts import CYBERDROP_PROVIDERS

BUILTIN_PROVIDERS = [TransferItProvider, MegaProvider, GofileProvider, MediaFireProvider,
                     GoogleDriveProvider, PixeldrainProvider, OneFichierProvider,
                     KrakenfilesProvider, CyberdropProvider, *CYBERDROP_PROVIDERS,
                     GenericProvider]
