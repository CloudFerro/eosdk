"""EOData access backends (SPEC §6.6): HTTP and S3."""

from eosdk.eodata.base import BaseDownloader, Downloader, DownloadReport, ProgressEvent
from eosdk.eodata.http import HttpDownloader
from eosdk.eodata.s3 import S3Downloader

__all__ = [
    "BaseDownloader",
    "DownloadReport",
    "Downloader",
    "HttpDownloader",
    "ProgressEvent",
    "S3Downloader",
]
