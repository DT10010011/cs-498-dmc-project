"""Database connection and helpers shared by every query.

The connection string comes from the MONGO_URI environment variable (put it in a .env file
next to app.py). Data lives in the `eurovision` database, `tweets` collection, in the flat
format written by loader.py (user_id, screen_name, name, user_verified, country_code, ...).
"""
import os

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import OperationFailure

load_dotenv()

_client = None
# Free/shared Atlas tiers (M0) reject allowDiskUse. We try it once; if the server refuses,
# we remember that and run every later pipeline without it (100 MB per-stage memory limit).
_disk_use_ok = True


def get_db():
    """Return the eurovision database, creating one shared MongoClient on first use."""
    global _client
    if _client is None:
        _client = MongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=10_000)
    return _client[os.environ.get("MONGO_DB", "eurovision")]


def _is_disk_use_error(e):
    return "allowDiskUse" in str(e) or "allowdiskuse" in str(e).lower()


def aggregate(collection, pipeline):
    """Run an aggregation pipeline and return the results as a list.

    Uses allowDiskUse when the cluster tier supports it, so big $group/$sort stages can spill
    to disk instead of failing at the 100 MB memory limit.
    """
    global _disk_use_ok
    if _disk_use_ok:
        try:
            return list(collection.aggregate(pipeline, allowDiskUse=True))
        except OperationFailure as e:
            if not _is_disk_use_error(e):
                raise
            _disk_use_ok = False  # this tier doesn't allow it; fall through and retry without
    return list(collection.aggregate(pipeline))


def explain_aggregate(db, pipeline, collection="tweets"):
    """Return MongoDB's explain output (executionStats) for an aggregation pipeline.

    Note: executionStats actually runs the query, so this takes as long as the query itself.
    """
    global _disk_use_ok
    cmd = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    if _disk_use_ok:
        cmd["allowDiskUse"] = True
    try:
        return db.command("explain", cmd, verbosity="executionStats")
    except OperationFailure as e:
        if "allowDiskUse" in cmd and _is_disk_use_error(e):
            _disk_use_ok = False
            del cmd["allowDiskUse"]
            return db.command("explain", cmd, verbosity="executionStats")
        raise


# Aggregation stage names as they appear in explain output ($cursor = the part pushed down
# to the query engine, which is where index use shows up).
AGG_STAGE_NAMES = {"$cursor", "$match", "$group", "$sort", "$limit", "$project", "$unwind",
                   "$graphLookup", "$lookup", "$facet", "$addFields", "$set", "$replaceRoot"}


def summarize_explain(explain):
    """Pull the useful parts out of a (deeply nested) explain document.

    Returns the aggregation stages, the winning plan's stages in order (e.g. FETCH -> IXSCAN),
    the indexes used, and the execution counters (keys/docs examined, time).
    """
    agg_stages, plan_stages, indexes, stats = [], [], [], {}
    counters = ("nReturned", "totalKeysExamined", "totalDocsExamined", "executionTimeMillis")

    def walk(node, in_plan=False):
        if isinstance(node, dict):
            for key in counters:
                if key in node and key not in stats:
                    stats[key] = node[key]
            if in_plan and isinstance(node.get("stage"), str):
                plan_stages.append(node["stage"])
            if in_plan and node.get("indexName") and node["indexName"] not in indexes:
                indexes.append(node["indexName"])
            for key, value in node.items():
                if key in ("rejectedPlans", "allPlansExecution", "command", "originalCommand"):
                    continue  # only describe the plan MongoDB chose, not the request we sent
                if key in AGG_STAGE_NAMES and key not in agg_stages and not in_plan:
                    agg_stages.append(key)
                walk(value, in_plan or key == "winningPlan")
        elif isinstance(node, list):
            for item in node:
                walk(item, in_plan)

    walk(explain)
    return {"agg_stages": agg_stages, "plan_stages": plan_stages, "indexes": indexes, "stats": stats}


def collection_stats(db, collection="tweets"):
    """Size information for the overview page (None if the tier doesn't allow collStats)."""
    try:
        s = db.command("collStats", collection)
    except Exception:
        return None
    return {
        "count": s.get("count"),
        "size_mb": s.get("size", 0) / 1e6,
        "storage_mb": s.get("storageSize", 0) / 1e6,
        "index_mb": s.get("totalIndexSize", 0) / 1e6,
        "avg_doc_bytes": s.get("avgObjSize"),
        "index_sizes": {k: v / 1e6 for k, v in (s.get("indexSizes") or {}).items()},
    }
