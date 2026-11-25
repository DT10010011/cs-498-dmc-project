"""The six project queries.

These follow the Stage 2 designs, adapted to the flat field names that loader.py actually wrote
(screen_name instead of user.screen_name, user_verified instead of user.verified, country_code
instead of country, user_id instead of user.id).

Every query is a Query object with:
  params    - the inputs shown as a form on the query's page
  pipeline  - builds the MongoDB pipeline (or find filter) from those inputs; shown on the page
  run       - executes the query and returns a result dict the template renders as a table
  explain   - returns MongoDB's explain() output for the query's main database step
"""
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from db import aggregate, explain_aggregate

TWEET_TYPES = ["simple", "reply", "quote", "retweet"]


@dataclass
class Param:
    name: str
    label: str
    default: object
    kind: str = "int"       # "int" or "str"
    min: int = 1
    max: int = 1000

    def parse(self, raw):
        """Turn the form value into a safe value (falls back to the default if invalid)."""
        if raw is None or str(raw).strip() == "":
            return self.default
        if self.kind == "str":
            return str(raw).strip().lstrip("@")[:50]
        try:
            return max(self.min, min(self.max, int(raw)))
        except ValueError:
            return self.default


@dataclass
class Query:
    qid: str
    title: str
    question: str
    strategy: str           # short explanation shown on the page (index used, approach)
    params: list
    pipeline: Callable      # params dict -> pipeline/filter (for display and execution)
    run: Callable           # (db, params) -> result dict
    explain: Callable       # (db, params) -> explain document
    pipeline_lang: str = "aggregate"
    extra_steps: list = field(default_factory=list)  # follow-up steps done in Python

    def parse_params(self, args):
        return {p.name: p.parse(args.get(p.name)) for p in self.params}


def table(columns, rows, summary=None, indent=None):
    """The result shape every template understands."""
    return {"columns": columns, "rows": rows, "summary": summary or [], "indent": indent}


def fmt_time(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


# ---------------------------------------------------------------------------------------------
# Q1: the thread(s) of tweets started by a user (default blcklcfr)
# ---------------------------------------------------------------------------------------------
def q1_pipeline(p):
    return [
        # all of the user's tweets (uses the screen_name_1_created_at_1 index)
        {"$match": {"screen_name": p["screen_name"]}},
        # follow replies recursively: tweets whose in_reply_to_status_id is this _id, then
        # replies to those, and so on. restrictSearchWithMatch repeats the partial index's
        # filter so every lookup can use the in_reply_to_status_id_1 partial index.
        {"$graphLookup": {
            "from": "tweets", "startWith": "$_id",
            "connectFromField": "_id", "connectToField": "in_reply_to_status_id",
            "as": "replies",
            "restrictSearchWithMatch": {"in_reply_to_status_id": {"$exists": True}},
        }},
        # put each starting tweet and its replies into one list
        {"$project": {"thread": {"$concatArrays": [
            [{"_id": "$_id", "text": "$text", "created_at": "$created_at",
              "in_reply_to_status_id": "$in_reply_to_status_id",
              "screen_name": "$screen_name", "name": "$name"}],
            "$replies",
        ]}}},
        {"$unwind": "$thread"},
        # a tweet can be reached from more than one of the user's tweets: keep one copy
        {"$group": {"_id": "$thread._id", "t": {"$first": "$thread"}}},
        # order the whole thread by time
        {"$sort": {"t.created_at": 1}},
        {"$project": {"_id": 1, "text": "$t.text", "time": "$t.created_at",
                      "in_reply_to": "$t.in_reply_to_status_id",
                      "name": "$t.name", "screen_name": "$t.screen_name"}},
    ]


def q1_run(db, p):
    docs = aggregate(db.tweets, q1_pipeline(p))
    # Rebuild the reply tree in Python so the page can indent replies under their parent.
    by_id = {d["_id"]: d for d in docs}
    children, roots = defaultdict(list), []
    for d in docs:  # already sorted by time, so children stay in time order
        parent = d.get("in_reply_to")
        (children[parent] if parent in by_id else roots).append(d)

    ordered, stack = [], [(r, 0) for r in reversed(roots)]
    while stack:  # depth-first walk, iterative so long threads can't hit the recursion limit
        d, depth = stack.pop()
        ordered.append((d, depth))
        stack.extend((c, depth + 1) for c in reversed(children[d["_id"]]))

    rows = [[fmt_time(d.get("time")), "@" + d.get("screen_name", ""), d.get("text", ""),
             str(d["_id"]), str(d.get("in_reply_to") or "")] for d, _ in ordered]
    by_user = sum(1 for d in docs if d.get("screen_name") == p["screen_name"])
    summary = [f"{len(docs)} tweets in the thread(s): {by_user} by @{p['screen_name']}, "
               f"{len(docs) - by_user} by others; {len(roots)} thread start(s)."]
    if not docs:
        summary = [f"No tweets found for @{p['screen_name']} (screen names are case-sensitive)."]
    return table(["Time (UTC)", "User", "Text", "Tweet id", "In reply to"], rows, summary,
                 indent=[depth for _, depth in ordered])


# ---------------------------------------------------------------------------------------------
# Q2: country with the most tweets
# ---------------------------------------------------------------------------------------------
def q2_pipeline(p):
    return [
        # only tweets with a location; this filter matches the partial index's filter exactly,
        # so MongoDB reads the small country_code_1 index instead of the whole collection
        {"$match": {"country_code": {"$exists": True}}},
        # count tweets per country (country holds the full name for display)
        {"$group": {"_id": "$country_code", "country": {"$first": "$country"},
                    "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": p["top"]},
    ]


def q2_run(db, p):
    docs = aggregate(db.tweets, q2_pipeline(p))
    located = db.tweets.count_documents({"country_code": {"$exists": True}})
    rows = [[i + 1, d.get("country") or "", d["_id"], f"{d['count']:,}"] for i, d in enumerate(docs)]
    summary = []
    if docs:
        summary.append(f"Answer: {docs[0].get('country')} ({docs[0]['_id']}) with "
                       f"{docs[0]['count']:,} tweets.")
    summary.append(f"Only {located:,} tweets have a location (place.country_code).")
    return table(["Rank", "Country", "Code", "Tweets"], rows, summary)


# ---------------------------------------------------------------------------------------------
# Q3: user with the most tweets
# ---------------------------------------------------------------------------------------------
def q3_pipeline(p):
    return [
        # Sorting on user_id first lets MongoDB answer from the user_id_1 index alone
        # (a covered index scan of ~12 MB) instead of reading every full tweet document.
        {"$sort": {"user_id": 1}},
        # count tweets per user; only the id and count are kept, so the $group stays small
        # enough for the 100 MB memory limit on the free tier
        {"$group": {"_id": "$user_id", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": p["top"]},
    ]


def q3_run(db, p):
    docs = aggregate(db.tweets, q3_pipeline(p))
    rows = []
    for i, d in enumerate(docs):
        # second step: look up each top user's name with the user_id_1 index
        u = db.tweets.find_one({"user_id": d["_id"]}, {"screen_name": 1, "name": 1}) or {}
        rows.append([i + 1, "@" + u.get("screen_name", "?"), u.get("name", ""), str(d["_id"]),
                     f"{d['count']:,}"])
    summary = [f"Answer: {rows[0][1]} ({rows[0][2]}) with {rows[0][4]} tweets."] if rows else []
    return table(["Rank", "Screen name", "Name", "User id", "Tweets"], rows, summary)


# ---------------------------------------------------------------------------------------------
# Q4: top 100 hashtags
# ---------------------------------------------------------------------------------------------
def q4_pipeline(p):
    return [
        # one row per (tweet, hashtag); tweets without hashtags have no field and are skipped.
        # Hashtags were lower-cased and de-duplicated per tweet by the loader, and retweets got
        # the original tweet's hashtags.
        {"$unwind": "$hashtags"},
        {"$group": {"_id": "$hashtags", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": p["top"]},
    ]


def q4_run(db, p):
    docs = aggregate(db.tweets, q4_pipeline(p))
    rows = [[i + 1, "#" + d["_id"], f"{d['count']:,}"] for i, d in enumerate(docs)]
    return table(["Rank", "Hashtag", "Tweets"], rows,
                 [f"Top {len(rows)} hashtags by number of tweets using them."])


# ---------------------------------------------------------------------------------------------
# Q5: trios of users who have all replied to each other
# ---------------------------------------------------------------------------------------------
Q5_FILTER = {"in_reply_to_status_id": {"$exists": True}}
Q5_PROJECTION = {"_id": 0, "user_id": 1, "screen_name": 1, "in_reply_to_user_id": 1}


def q5_pipeline(p):
    # Q5 is a find() plus Python, so this is the find that is sent to MongoDB.
    return {"filter": Q5_FILTER, "projection": Q5_PROJECTION}


def find_trios(replies):
    """Given (replier_id, replied_to_id) pairs, return sorted (a, b, c) trios where every pair
    of the three has replied to each other in both directions."""
    replied = {(a, b) for a, b in replies if b is not None and a != b}  # ignore self-replies
    # keep only mutual pairs, stored as an adjacency list (an undirected "mutual reply" graph)
    neighbours = defaultdict(set)
    for a, b in replied:
        if (b, a) in replied:
            neighbours[a].add(b)
    # a trio is a triangle in that graph. For each edge a<b, any common neighbour c>b closes a
    # triangle; the a<b<c ordering means each trio is reported exactly once.
    trios = []
    for a in neighbours:
        for b in neighbours[a]:
            if b > a:
                for c in neighbours[a] & neighbours[b]:
                    if c > b:
                        trios.append((a, b, c))
    return sorted(trios), len(replied), sum(len(n) for n in neighbours.values()) // 2


def q5_run(db, p):
    names, pairs, n_replies = {}, [], 0
    # 1. read every reply (the filter matches the in_reply_to_status_id_1 partial index, which
    #    only contains replies, so non-reply tweets are never read)
    for t in db.tweets.find(Q5_FILTER, Q5_PROJECTION):
        n_replies += 1
        names[t["user_id"]] = t.get("screen_name", "?")
        pairs.append((t["user_id"], t.get("in_reply_to_user_id")))
    # 2-3. mutual pairs and triangles, done in Python
    trios, n_pairs, n_mutual = find_trios(pairs)
    shown = trios[: p["top"]]
    rows = [[i + 1] + [f"@{names.get(u, '?')} ({u})" for u in trio] for i, trio in enumerate(shown)]
    summary = [f"Scanned {n_replies:,} replies -> {n_pairs:,} distinct (replier, replied-to) pairs "
               f"-> {n_mutual:,} mutual pairs -> {len(trios):,} trios."]
    if len(trios) > len(shown):
        summary.append(f"Showing the first {len(shown)}.")
    return table(["#", "User A", "User B", "User C"], rows, summary)


def q5_explain(db, p):
    return db.tweets.find(Q5_FILTER, Q5_PROJECTION).explain()


# ---------------------------------------------------------------------------------------------
# Q6: percentage of each tweet type sent by verified users
# ---------------------------------------------------------------------------------------------
def q6_pipeline(p):
    return [
        # verified users only. user_verified, user_id and type are all in the compound index
        # user_verified_1_user_id_1_type_1, so this can be answered from the index alone
        # (a covered query) without reading the tweet documents.
        {"$match": {"user_verified": True}},
        # count tweets per user per type
        {"$group": {"_id": {"user": "$user_id", "type": "$type"}, "n": {"$sum": 1}}},
        # per user: total tweets and the count for each type
        {"$group": {"_id": "$_id.user", "total": {"$sum": "$n"},
                    "types": {"$push": {"type": "$_id.type", "n": "$n"}}}},
        # percent = count of that type / total * 100
        {"$project": {"total": 1, "types": 1, "percentages": {"$map": {
            "input": "$types", "as": "t",
            "in": {"type": "$$t.type",
                   "pct": {"$multiply": [100, {"$divide": ["$$t.n", "$total"]}]}}}}}},
        {"$sort": {"total": -1}},
    ]


def q6_run(db, p):
    docs = aggregate(db.tweets, q6_pipeline(p))
    # Overall percentages across all verified users (sum of every user's counts).
    overall = defaultdict(int)
    for d in docs:
        for t in d["types"]:
            overall[t["type"]] += t["n"]
    grand = sum(overall.values()) or 1
    summary = [f"{len(docs):,} verified users sent {grand:,} tweets. Overall: " + ", ".join(
        f"{t} {100 * overall[t] / grand:.1f}%" for t in TWEET_TYPES)]

    # Look up screen names only for the users we display (user_id_1 index).
    top = docs[: p["top"]]
    ids = [d["_id"] for d in top]
    names = {n["_id"]: n["screen_name"] for n in aggregate(db.tweets, [
        {"$match": {"user_id": {"$in": ids}}},
        {"$group": {"_id": "$user_id", "screen_name": {"$first": "$screen_name"}}},
    ])} if ids else {}

    rows = []
    for d in top:
        pct = {t["type"]: t["pct"] for t in d["percentages"]}
        rows.append(["@" + names.get(d["_id"], "?"), f"{d['total']:,}"] +
                    [f"{pct.get(t, 0):.1f}%" for t in TWEET_TYPES])
    if len(docs) > len(top):
        summary.append(f"Table shows the {len(top)} verified users with the most tweets.")
    return table(["User", "Tweets"] + [t.capitalize() + " %" for t in TWEET_TYPES], rows, summary)


# ---------------------------------------------------------------------------------------------
def agg_explain(pipeline_fn):
    return lambda db, p: explain_aggregate(db, pipeline_fn(p))


QUERIES = {q.qid: q for q in [
    Query("q1", "Q1: Reply threads",
          "Show the thread(s) of tweets started by a user, with all replies, ordered by time.",
          "Index screen_name_1_created_at_1 finds the user's tweets; $graphLookup follows "
          "replies through the partial index in_reply_to_status_id_1.",
          [Param("screen_name", "Screen name", "blcklcfr", kind="str")],
          q1_pipeline, q1_run, agg_explain(q1_pipeline)),
    Query("q2", "Q2: Top country",
          "Which country has the most tweets?",
          "The $match filter matches the partial index country_code_1, so only tweets that "
          "have a location are read.",
          [Param("top", "Show top N countries", 10, max=300)],
          q2_pipeline, q2_run, agg_explain(q2_pipeline)),
    Query("q3", "Q3: Top user",
          "Which user posted the most tweets?",
          "A covered scan of index user_id_1 (no documents fetched), then $group. Names "
          "for the top users are looked up afterwards with the same index.",
          [Param("top", "Show top N users", 10, max=200)],
          q3_pipeline, q3_run, agg_explain(q3_pipeline),
          extra_steps=["For each result: db.tweets.findOne({user_id: <id>}, "
                       "{screen_name: 1, name: 1})"]),
    Query("q4", "Q4: Top hashtags",
          "What are the 100 most used hashtags?",
          "No index helps: every tweet's hashtags must be read ($unwind + $group over the "
          "whole collection).",
          [Param("top", "Show top N hashtags", 100, max=1000)],
          q4_pipeline, q4_run, agg_explain(q4_pipeline)),
    Query("q5", "Q5: Mutual-reply trios",
          "Find trios of users who have all replied to each other.",
          "find() over the partial index in_reply_to_status_id_1 (replies only), then Python "
          "builds the mutual-reply graph and finds its triangles.",
          [Param("top", "Show at most N trios", 200, max=5000)],
          q5_pipeline, q5_run, q5_explain, pipeline_lang="find",
          extra_steps=["Python: keep (replier, replied-to) pairs, keep only mutual pairs, "
                       "then find triangles a < b < c in the mutual graph."]),
    Query("q6", "Q6: Verified users' tweet types",
          "For verified users, what percentage of their tweets are simple tweets, replies, "
          "quotes and retweets?",
          "Covered query on the compound index user_verified_1_user_id_1_type_1. Screen "
          "names for the displayed users are looked up with user_id_1.",
          [Param("top", "Show top N users", 50, max=2000)],
          q6_pipeline, q6_run, agg_explain(q6_pipeline),
          extra_steps=["db.tweets.aggregate([{$match: {user_id: {$in: <shown ids>}}}, "
                       "{$group: {_id: '$user_id', screen_name: {$first: '$screen_name'}}}])"]),
]}
