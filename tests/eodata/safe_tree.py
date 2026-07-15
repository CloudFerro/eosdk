"""A realistic (miniature) SAFE product tree, shared by the S3 and HTTP tests.

The same logical structure feeds moto-seeded S3 buckets and download-service Nodes
fixtures, so the cross-backend parity test compares like with like.
"""

PRODUCT_NAME = "S2B_MSIL2A_20260615T095029_N0511_R079_T34UEE_20260615T105512.SAFE"
BUCKET = "eodata"
PREFIX = f"Sentinel-2/MSI/L2A/2026/06/15/{PRODUCT_NAME}"
S3_PATH = f"/{BUCKET}/{PREFIX}"

# logical path (relative to product root) -> content bytes
FILES: dict[str, bytes] = {
    "manifest.safe": b"<manifest/>" * 40,
    "MTD_MSIL2A.xml": b"<metadata/>" * 64,
    "GRANULE/L2A_T34UEE_A012345_20260615T095030/MTD_TL.xml": b"<tile/>" * 32,
    "GRANULE/L2A_T34UEE_A012345_20260615T095030/IMG_DATA/R10m/T34UEE_B04_10m.jp2": (
        b"\xff\x4f\xff\x51" + b"B04" * 5000
    ),
    "GRANULE/L2A_T34UEE_A012345_20260615T095030/IMG_DATA/R10m/T34UEE_B08_10m.jp2": (
        b"\xff\x4f\xff\x51" + b"B08" * 5000
    ),
}

# directories implied by FILES, as logical paths
DIRECTORIES = [
    "GRANULE",
    "GRANULE/L2A_T34UEE_A012345_20260615T095030",
    "GRANULE/L2A_T34UEE_A012345_20260615T095030/IMG_DATA",
    "GRANULE/L2A_T34UEE_A012345_20260615T095030/IMG_DATA/R10m",
]

ROOT_CHILDREN = ["GRANULE", "MTD_MSIL2A.xml", "manifest.safe"]
