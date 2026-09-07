# MongoDB Recovery Driver

The MongoDB driver provides document-level and field-level recoverability across MongoDB 6+ replica sets and standalone instances.

---

## 1. Supported Operations

- **Document Insert**: Reverted via explicit document removal matching the generated `_id`.
- **Field Updates (`$set`, `$inc`)**: Reverted by restoring pre-image field values or executing `$unset` for newly introduced fields.
- **Document Deletion**: Reverted by re-inserting the exact pre-image BSON document.

---

## 2. Example

```python
from pymongo import MongoClient
from evoundo import protect_tool, revert

client = MongoClient("mongodb://localhost:27017")
db = client["production_db"]

@protect_tool(
    target="mongodb://production_db/deployments/{service_name}",
    surface="mongodb",
)
def update_service_replicas(service_name: str, replica_count: int):
    col = db["deployments"]
    col.update_one(
        {"service": service_name},
        {"$set": {"replicas": replica_count}},
        upsert=True,
    )
    return {"service": service_name, "replicas": replica_count}
```

---

## 3. Conflict Gating

If an intervening mutation updates the exact same document field, EvoUndo detects field-level collisions and refuses unsafe selective rollback.
