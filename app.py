"""CS 498 DMC Project Stage 3 - web front end for our MongoDB Eurovision 2018 tweets prototype.

Run locally:   python3 app.py            (then open http://localhost:8000)
Run on the VM: gunicorn -w 1 --threads 4 --timeout 600 -b 0.0.0.0:8000 app:app

Pages:
  /           overview: data model, a sample document, collection and index sizes
  /q/<qid>    one page per query (q1..q6): inputs, the MongoDB pipeline, results, explain plan
"""
import json
import os
import threading
import time
from datetime import datetime

from flask import Flask, abort, render_template, request

from db import aggregate, collection_stats, get_db, summarize_explain
from queries import QUERIES

app = Flask(__name__)

# Simple in-memory cache: full-collection queries (Q3, Q4, Q6) take a while on a small cluster,
# so a result is kept until someone ticks "run again". Key = (query id, action, params).
_cache = {}
_cache_lock = threading.Lock()


def cached(key, compute, fresh=False):
    """Return {"value", "seconds", "at", "from_cache"} for key, computing it if needed."""
    with _cache_lock:
        hit = None if fresh else _cache.get(key)
    if hit:
        return {**hit, "from_cache": True}
    start = time.perf_counter()
    value = compute()
    entry = {"value": value, "seconds": time.perf_counter() - start, "at": datetime.now()}
    with _cache_lock:
        _cache[key] = entry
    return {**entry, "from_cache": False}


def to_json(obj):
    """Pretty-print a pipeline / explain document (tweet ids and dates become strings)."""
    return json.dumps(obj, indent=2, default=str, ensure_ascii=False)


@app.context_processor
def inject_nav():
    # every template gets the query list for the navigation bar
    return {"queries": QUERIES}


@app.route("/")
def overview():
    db = get_db()
    error = None
    try:
        count = db.tweets.estimated_document_count()
        stats = collection_stats(db)
        # show the tweet used in the Stage 2 report as the example document
        sample = (db.tweets.find_one({"_id": 995332494185680897})
                  or db.tweets.find_one({"type": "reply"}))
        breakdown = None
        if request.args.get("breakdown"):
            # tweets per type: a full scan, so only on request and cached
            breakdown = cached(("overview", "types"), lambda: aggregate(db.tweets, [
                {"$group": {"_id": "$type", "n": {"$sum": 1}}}, {"$sort": {"n": -1}}]),
                fresh=bool(request.args.get("fresh")))
    except Exception as e:  # e.g. Atlas network access not set up for this machine
        count, stats, sample, breakdown = None, None, None, None
        error = f"{type(e).__name__}: {e}"
    return render_template("index.html", count=count, stats=stats, breakdown=breakdown,
                           sample=to_json(sample) if sample else None, error=error)


@app.route("/q/<qid>")
def query_page(qid):
    q = QUERIES.get(qid) or abort(404)
    params = q.parse_params(request.args)
    action = request.args.get("action")          # None, "run" or "explain"
    fresh = bool(request.args.get("fresh"))      # bypass the cache
    key = (qid, action, tuple(sorted(params.items())))
    result = explain = error = None
    try:
        if action == "run":
            result = cached(key, lambda: q.run(get_db(), params), fresh)
        elif action == "explain":
            explain = cached(key, lambda: q.explain(get_db(), params), fresh)
            explain["summary"] = summarize_explain(explain["value"])
            explain["json"] = to_json(explain["value"])
    except Exception as e:  # show MongoDB errors (e.g. memory limits) on the page
        error = f"{type(e).__name__}: {e}"
    return render_template("query.html", q=q, params=params, action=action,
                           pipeline=to_json(q.pipeline(params)), result=result,
                           explain=explain, error=error)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
