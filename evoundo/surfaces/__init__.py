"""EvoUndo External Mutation Surfaces.

Exposes drivers for Amazon S3, MongoDB, Microsoft SQL Server,
Azure Blob Storage, and Google Cloud Storage.
"""

from evoundo.surfaces.s3 import S3SurfaceDriver
from evoundo.surfaces.mongodb import MongoSurfaceDriver
from evoundo.surfaces.sqlserver import SqlServerSurfaceDriver
from evoundo.surfaces.blob_storage import AzureBlobSurfaceDriver, GcsSurfaceDriver

__all__ = [
    "S3SurfaceDriver",
    "MongoSurfaceDriver",
    "SqlServerSurfaceDriver",
    "AzureBlobSurfaceDriver",
    "GcsSurfaceDriver",
    "register_all_surfaces",
]


def register_all_surfaces() -> None:
    """Register all Phase 4 mutation surface drivers with the central DriverRegistry."""
    S3SurfaceDriver.register()
    MongoSurfaceDriver.register()
    SqlServerSurfaceDriver.register()
    AzureBlobSurfaceDriver.register()
    GcsSurfaceDriver.register()


# Auto-register on import
register_all_surfaces()
