"""EOData access backends (SPEC §6.6); Exos/S3 lands in Phase 2."""

from eosdk.eodata.base import BaseDownloader, Downloader, DownloadReport, ProgressEvent
from eosdk.eodata.zipper import ZipperDownloader

__all__ = [
    "BaseDownloader",
    "DownloadReport",
    "Downloader",
    "ProgressEvent",
    "ZipperDownloader",
]
