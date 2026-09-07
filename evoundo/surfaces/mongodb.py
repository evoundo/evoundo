"""MongoDB mutation surface driver for EvoUndo.

Provides document and field-level recovery, optimistic concurrency,
conflict refusal, array mutation inversion, multi-document transactions,
and physical MongoDB external state verification.
"""

from __future__ import annotations
from contextlib import contextmanager
import copy
import json
import logging
import os
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, Union

from evoundo.recovery.driver_registry import DriverRegistry

logger = logging.getLogger("evoundo.surfaces.mongodb")


def get_dotted_path(doc: Dict[str, Any], path: str, default: Any = None) -> Any:
    """Retrieve value from a nested dict using a dotted path or direct key."""
    if not doc or not path:
        return default
    if path in doc:
        return doc[path]
    parts = path.split(".")
    curr: Any = doc
    for part in parts:
        if isinstance(curr, dict) and part in curr:
            curr = curr[part]
        else:
            return default
    return curr


def has_dotted_path(doc: Dict[str, Any], path: str) -> bool:
    """Check if a dotted path or direct key exists within a document."""
    if not doc or not path:
        return False
    if path in doc:
        return True
    parts = path.split(".")
    curr: Any = doc
    for part in parts:
        if isinstance(curr, dict) and part in curr:
            curr = curr[part]
        else:
            return False
    return True


def set_dotted_path(doc: Dict[str, Any], path: str, value: Any) -> None:
    """Set value at a dotted path within a nested dict."""
    parts = path.split(".")
    curr = doc
    for part in parts[:-1]:
        if part not in curr or not isinstance(curr[part], dict):
            curr[part] = {}
        curr = curr[part]
    curr[parts[-1]] = value


class MongoSurfaceDriver:
    """Production MongoDB mutation driver with document and field-level recovery."""

    DRIVER_TYPE = "mongodb"
    _client_cache: Dict[str, Any] = {}

    @classmethod
    def get_client(cls, uri: Optional[str] = None) -> Any:
        """Construct or retrieve cached MongoClient.
        
        Never silently falls back to mongomock when a real URI or default is targeted.
        Fails closed with ConnectionError if real MongoDB is unreachable.
        """
        target_uri = uri or os.environ.get(
            "EVOUNDO_MONGO_URI", "mongodb://127.0.0.1:27017/?replicaSet=rs0&directConnection=true"
        )
        if target_uri in cls._client_cache:
            return cls._client_cache[target_uri]

        if target_uri.startswith("mongomock://") or os.environ.get("EVOUNDO_USE_MONGOMOCK") == "1":
            try:
                import mongomock
                client = mongomock.MongoClient()
                cls._client_cache[target_uri] = client
                return client
            except Exception as e:
                raise ConnectionError(f"Failed to initialize mongomock client: {e}") from e

        try:
            import pymongo
            client = pymongo.MongoClient(target_uri, serverSelectionTimeoutMS=3000)
            # Verify connectivity immediately via ping command
            client.admin.command("ping")
            cls._client_cache[target_uri] = client
            return client
        except Exception as e:
            logger.error("Failed to connect to real MongoDB instance at '%s': %s", target_uri, e)
            raise ConnectionError(f"Failed to connect to real MongoDB at {target_uri}: {e}") from e

    @classmethod
    @contextmanager
    def transaction_scope(cls, uri: Optional[str] = None, client: Optional[Any] = None) -> Iterator[Any]:
        """Context manager providing an ACID multi-document transaction session."""
        mongo = client or cls.get_client(uri)
        session = mongo.start_session()
        try:
            with session.start_transaction():
                yield session
        finally:
            session.end_session()

    @classmethod
    def register(cls) -> None:
        """Register this driver with the central DriverRegistry."""
        DriverRegistry.register_driver(
            driver_type=cls.DRIVER_TYPE,
            executor=cls.execute_recovery,
            verifier=cls.verify_recovery,
        )
        logger.info("Registered '%s' surface driver with DriverRegistry", cls.DRIVER_TYPE)

    @classmethod
    def capture_witness(
        cls,
        db_name: str,
        collection_name: str,
        doc_filter: Dict[str, Any],
        client: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Capture pre-mutation document snapshot for a MongoDB collection."""
        mongo = client or cls.get_client()
        col = mongo[db_name][collection_name]
        doc = col.find_one(doc_filter)
        if doc is None:
            return {
                "db_name": db_name,
                "collection_name": collection_name,
                "existed": False,
                "filter": doc_filter,
                "document": None,
            }
        return {
            "db_name": db_name,
            "collection_name": collection_name,
            "existed": True,
            "filter": doc_filter,
            "doc_id": str(doc.get("_id")),
            "document": copy.deepcopy(doc),
        }

    @classmethod
    def execute_recovery(cls, op: Any, witness: Any) -> None:
        """Execute physical rollback of a MongoDB mutation."""
        params = op.parameters or {}
        db_name = params.get("db_name", "test")
        col_name = params.get("collection_name", "records")
        operation = (op.operation or params.get("operation", "INSERT_ONE")).upper()
        session = params.get("session")
        if session and hasattr(session, "client") and session.client:
            mongo = session.client
        else:
            mongo = cls.get_client(params.get("mongo_uri"))
        col = mongo[db_name][col_name]

        w = op.witness_data or params.get("witness", {})
        if hasattr(w, "data") and isinstance(w.data, dict):
            w = w.data
        elif hasattr(witness, "data") and isinstance(witness.data, dict) and not w:
            w = witness.data

        if operation in ("INSERT_ONE", "INSERT"):
            doc_id = params.get("doc_id")
            if doc_id is None and isinstance(w, dict) and "doc_id" in w:
                doc_id = w["doc_id"]

            if doc_id is not None:
                # Downstream conflict check
                curr = col.find_one({"_id": doc_id}, session=session)
                expected_inserted = params.get("inserted_document")
                if curr and expected_inserted and isinstance(expected_inserted, dict):
                    # If fields changed downstream, refuse with conflict
                    for k, v in expected_inserted.items():
                        if k != "_id" and get_dotted_path(curr, k) != v:
                            raise ValueError(
                                f"CONFLICT_DETECTED: MongoDB document '{doc_id}' in '{col_name}' "
                                f"was modified downstream (field '{k}' differs)"
                            )
                col.delete_one({"_id": doc_id}, session=session)
                logger.info("Deleted inserted MongoDB document '%s' in '%s.%s'", doc_id, db_name, col_name)

        elif operation in ("UPDATE_ONE", "UPDATE"):
            doc_id = params.get("doc_id")
            existed = w.get("existed", True)
            orig_doc = w.get("document")

            if doc_id and existed and orig_doc:
                curr = col.find_one({"_id": doc_id}, session=session)
                if curr is None:
                    raise ValueError(
                        f"CONFLICT_DETECTED: Target document '{doc_id}' deleted downstream in '{col_name}'"
                    )

                # Check if fields touched by intervening updates conflict
                modified_fields = params.get("modified_fields", [])
                expected_post = params.get("expected_post_values", {})
                for field in modified_fields:
                    if field in expected_post and get_dotted_path(curr, field) != expected_post[field]:
                        raise ValueError(
                            f"CONFLICT_DETECTED: Field '{field}' on document '{doc_id}' was "
                            f"modified downstream by another worker"
                        )

                # Differential restoration: use $set on pre-existing values, $unset on added fields
                set_dict: Dict[str, Any] = {}
                unset_dict: Dict[str, Any] = {}

                if modified_fields:
                    for field in modified_fields:
                        if has_dotted_path(orig_doc, field):
                            set_dict[field] = get_dotted_path(orig_doc, field)
                        else:
                            unset_dict[field] = ""
                else:
                    if params.get("replace_whole_document", False):
                        col.replace_one({"_id": doc_id}, orig_doc, session=session)
                        logger.info("Replaced entire MongoDB document '%s' in '%s.%s'", doc_id, db_name, col_name)
                        return

                    all_keys = set(k for k in orig_doc.keys() if k != "_id") | set(k for k in curr.keys() if k != "_id")
                    for k in all_keys:
                        if k in orig_doc:
                            set_dict[k] = orig_doc[k]
                        else:
                            unset_dict[k] = ""

                update_ops: Dict[str, Any] = {}
                if set_dict:
                    update_ops["$set"] = set_dict
                if unset_dict:
                    update_ops["$unset"] = unset_dict

                if update_ops:
                    col.update_one({"_id": doc_id}, update_ops, session=session)
                logger.info("Restored modified fields on MongoDB document '%s' in '%s.%s'", doc_id, db_name, col_name)

        elif operation in ("DELETE_ONE", "DELETE"):
            orig_doc = w.get("document")
            if orig_doc:
                # Check downstream collision: if another document with identical _id exists
                existing = col.find_one({"_id": orig_doc["_id"]}, session=session)
                if existing is not None:
                    raise ValueError(
                        f"CONFLICT_DETECTED: Document with _id '{orig_doc['_id']}' already exists downstream in '{col_name}'"
                    )
                # Re-insert original document
                col.replace_one({"_id": orig_doc["_id"]}, orig_doc, upsert=True, session=session)
                logger.info("Re-inserted deleted MongoDB document '%s' in '%s.%s'", orig_doc.get("_id"), db_name, col_name)

        elif operation in ("ARRAY_PUSH", "PUSH"):
            doc_id = params.get("doc_id")
            field = params.get("field")
            element = params.get("element")
            element_id_field = params.get("element_id_field")

            if not doc_id or not field:
                raise ValueError("ARRAY_PUSH recovery requires 'doc_id' and 'field'")

            curr = col.find_one({"_id": doc_id}, session=session)
            if curr is None:
                raise ValueError(f"CONFLICT_DETECTED: Target document '{doc_id}' not found in '{col_name}'")

            arr = get_dotted_path(curr, field)
            if not isinstance(arr, list):
                raise ValueError(f"CONFLICT_DETECTED: Field '{field}' is not an array in document '{doc_id}'")

            if element_id_field and isinstance(element, dict) and element_id_field in element:
                target_id = element[element_id_field]
                matches = [item for item in arr if isinstance(item, dict) and item.get(element_id_field) == target_id]
                if len(matches) == 0:
                    raise ValueError(f"CONFLICT_DETECTED: Pushed element with {element_id_field}={target_id} not found")
                col.update_one({"_id": doc_id}, {"$pull": {field: {element_id_field: target_id}}}, session=session)
            else:
                matches = [item for item in arr if item == element]
                if len(matches) == 0:
                    raise ValueError(f"CONFLICT_DETECTED: Target array element not found in '{field}'")
                if len(matches) > 1:
                    # Semantic Constraint 1: duplicate indistinguishable elements fail closed
                    raise ValueError(
                        f"CONFLICT_DETECTED: Indistinguishable duplicate array element cannot be safely removed from '{field}'"
                    )
                col.update_one({"_id": doc_id}, {"$pull": {field: element}}, session=session)
            logger.info("Reverted array push on '%s.%s' for document '%s'", col_name, field, doc_id)

        elif operation in ("ARRAY_PULL", "PULL"):
            doc_id = params.get("doc_id")
            field = params.get("field")
            element = params.get("element")
            if not doc_id or not field:
                raise ValueError("ARRAY_PULL recovery requires 'doc_id' and 'field'")
            col.update_one({"_id": doc_id}, {"$push": {field: element}}, session=session)
            logger.info("Reverted array pull on '%s.%s' for document '%s'", col_name, field, doc_id)

        elif operation in ("ARRAY_ADD_TO_SET", "ADD_TO_SET"):
            doc_id = params.get("doc_id")
            field = params.get("field")
            element = params.get("element")
            if not doc_id or not field:
                raise ValueError("ARRAY_ADD_TO_SET recovery requires 'doc_id' and 'field'")
            # If element was added, inverse is pulling it
            col.update_one({"_id": doc_id}, {"$pull": {field: element}}, session=session)
            logger.info("Reverted addToSet on '%s.%s' for document '%s'", col_name, field, doc_id)

        else:
            raise NotImplementedError(f"Unsupported MongoDB recovery operation: {operation}")

    @classmethod
    def verify_recovery(cls, op: Any, witness: Any) -> bool:
        """Physically verify external MongoDB state matches expected post-recovery condition."""
        params = op.parameters or {}
        db_name = params.get("db_name", "test")
        col_name = params.get("collection_name", "records")
        operation = (op.operation or params.get("operation", "INSERT_ONE")).upper()
        session = params.get("session")
        if session and hasattr(session, "client") and session.client:
            mongo = session.client
        else:
            mongo = cls.get_client(params.get("mongo_uri"))
        col = mongo[db_name][col_name]

        w = op.witness_data or params.get("witness", {})
        if hasattr(w, "data") and isinstance(w.data, dict):
            w = w.data
        elif hasattr(witness, "data") and isinstance(witness.data, dict) and not w:
            w = witness.data

        try:
            if operation in ("INSERT_ONE", "INSERT"):
                doc_id = params.get("doc_id")
                if doc_id is None and isinstance(w, dict):
                    doc_id = w.get("doc_id")
                if doc_id is not None:
                    return col.find_one({"_id": doc_id}, session=session) is None
                return True

            elif operation in ("UPDATE_ONE", "UPDATE"):
                doc_id = params.get("doc_id")
                orig_doc = w.get("document")
                if doc_id and orig_doc:
                    curr = col.find_one({"_id": doc_id}, session=session)
                    if curr is None:
                        return False
                    modified_fields = params.get("modified_fields")
                    if modified_fields:
                        for field in modified_fields:
                            if has_dotted_path(orig_doc, field):
                                if get_dotted_path(curr, field) != get_dotted_path(orig_doc, field):
                                    return False
                            else:
                                if has_dotted_path(curr, field):
                                    return False
                        return True
                    for k, v in orig_doc.items():
                        if curr.get(k) != v:
                            return False
                    return True
                return True

            elif operation in ("DELETE_ONE", "DELETE"):
                orig_doc = w.get("document")
                if orig_doc:
                    curr = col.find_one({"_id": orig_doc["_id"]}, session=session)
                    if curr is None:
                        return False
                    for k, v in orig_doc.items():
                        if curr.get(k) != v:
                            return False
                    return True
                return True

            elif operation in ("ARRAY_PUSH", "PUSH"):
                doc_id = params.get("doc_id")
                field = params.get("field")
                element = params.get("element")
                element_id_field = params.get("element_id_field")
                curr = col.find_one({"_id": doc_id}, session=session)
                if curr is None:
                    return False
                arr = get_dotted_path(curr, field) or []
                if element_id_field and isinstance(element, dict) and element_id_field in element:
                    target_id = element[element_id_field]
                    return not any(isinstance(item, dict) and item.get(element_id_field) == target_id for item in arr)
                return element not in arr

            elif operation in ("ARRAY_PULL", "PULL"):
                doc_id = params.get("doc_id")
                field = params.get("field")
                element = params.get("element")
                curr = col.find_one({"_id": doc_id}, session=session)
                if curr is None:
                    return False
                arr = get_dotted_path(curr, field) or []
                return element in arr

            elif operation in ("ARRAY_ADD_TO_SET", "ADD_TO_SET"):
                doc_id = params.get("doc_id")
                field = params.get("field")
                element = params.get("element")
                curr = col.find_one({"_id": doc_id}, session=session)
                if curr is None:
                    return False
                arr = get_dotted_path(curr, field) or []
                return element not in arr

            return True
        except Exception as e:
            logger.error("Failed MongoDB physical recovery verification: %s", e)
            return False
