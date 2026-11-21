"""In-memory stand-ins for vector database servers, used to run the shared contract suite offline.

Each fake follows the request/response shapes of the real API that the adapter uses (verified against the
vendors' docs, ADR-0028) and computes results with brute-force cosine / dot similarity. They exercise the
adapters' real code paths: request building, filters, upserts, dimension checks and response parsing. They
are not a substitute for the opt-in live tests against real servers.
"""

from __future__ import annotations

import copy
import json
import math
import re
import struct
from types import SimpleNamespace
from typing import Any

import httpx


def _sim(q, v, distance):
    dot = sum(a * b for a, b in zip(q, v, strict=False))
    if distance in ("dot", "dot_product", "max_inner_product", "innerproduct", "IP", "dotProduct"):
        return dot
    n = math.sqrt(sum(a * a for a in q)) * math.sqrt(sum(b * b for b in v))
    return dot / n if n else 0.0


# ---- Elasticsearch / OpenSearch (REST) -----------------------------------------------------------
class FakeSearchServer:
    def __init__(self, flavor: str) -> None:
        self.flavor = flavor
        self.indices: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        parts = [p for p in request.url.path.split("/") if p]
        method = request.method
        if parts == ["_bulk"]:
            return self._bulk(request)
        name = parts[0]
        idx = self.indices.get(name)
        if len(parts) == 1:
            if method == "HEAD":
                return httpx.Response(200 if idx else 404)
            if method == "PUT":
                if idx:
                    return httpx.Response(400, json={"error": "resource_already_exists_exception"})
                body = json.loads(request.content)
                self.indices[name] = {"mappings": body["mappings"], "settings": body.get("settings"), "docs": {}}
                return httpx.Response(200, json={"acknowledged": True})
            if method == "DELETE":
                self.indices.pop(name, None)
                return httpx.Response(200, json={"acknowledged": True})
        if idx is None:
            return httpx.Response(404, json={"error": "index_not_found_exception"})
        op = parts[1]
        if op == "_mapping":
            return httpx.Response(200, json={name: {"mappings": idx["mappings"]}})
        if op == "_count":
            return httpx.Response(200, json={"count": len(idx["docs"])})
        if op == "_mget":
            ids = json.loads(request.content)["ids"]
            include = request.url.params.get("_source_includes", "").split(",")
            docs = [
                {"_id": i, "found": i in idx["docs"],
                 **({"_source": {k: idx["docs"][i][k] for k in include}} if i in idx["docs"] else {})}
                for i in ids
            ]  # fmt: skip
            return httpx.Response(200, json={"docs": docs})
        if op == "_search":
            return self._search(idx, json.loads(request.content))
        return httpx.Response(400, json={"error": f"unsupported {method} {request.url.path}"})

    def _bulk(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["content-type"] == "application/x-ndjson"
        lines = [json.loads(line) for line in request.content.decode().splitlines() if line.strip()]
        items = []
        for action, source in zip(lines[::2], lines[1::2], strict=True):
            meta = action["index"]
            idx = self.indices[meta["_index"]]
            vec_field = idx["mappings"]["properties"]["vector"]
            dims = vec_field.get("dims") or vec_field.get("dimension")
            if len(source["vector"]) != dims:
                items.append({"index": {"_id": meta["_id"], "error": {"type": "illegal_argument_exception"}}})
                continue
            idx["docs"][meta["_id"]] = source
            items.append({"index": {"_id": meta["_id"], "result": "created"}})
        return httpx.Response(200, json={"errors": any("error" in i["index"] for i in items), "items": items})

    def _search(self, idx: dict[str, Any], body: dict[str, Any]) -> httpx.Response:
        vec_field = idx["mappings"]["properties"]["vector"]
        if self.flavor == "opensearch":
            knn = body["query"]["knn"]["vector"]
            q, k = knn["vector"], knn["k"]
            metric = vec_field["method"]["space_type"]
        else:
            knn = body["knn"]
            q, k = knn["query_vector"], knn["k"]
            assert knn["num_candidates"] >= k
            metric = vec_field["similarity"]
        terms = [t["term"] for t in (knn.get("filter", {}).get("bool", {}).get("filter", []))]
        scored = []
        for doc_id, src in idx["docs"].items():
            if all(src.get(f) == v for t in terms for f, v in t.items()):
                s = _sim(q, src["vector"], metric)
                scored.append((doc_id, (1 + s) / 2, src))  # cosine-style rescaling, as the engines do
        scored.sort(key=lambda x: -x[1])
        hits = [
            {"_id": i, "_score": s, "_source": {f: src.get(f) for f in body["_source"]}} for i, s, src in scored[:k]
        ]
        return httpx.Response(200, json={"hits": {"hits": hits}})


# ---- MongoDB Atlas ---------------------------------------------------------------------------------
class FakeMongoCollection:
    def __init__(self, db: FakeMongoDB, name: str) -> None:
        self.db, self.name = db, name
        self.docs: dict[str, dict[str, Any]] = {}
        self.search_index: dict[str, Any] | None = None
        self._polls = 0

    def find_one(self, query):
        d = self.docs.get(query["_id"])
        return copy.deepcopy(d) if d else None

    def insert_one(self, doc):
        self.docs[doc["_id"]] = copy.deepcopy(doc)

    def delete_one(self, query):
        self.docs.pop(query["_id"], None)

    def create_search_index(self, model):
        self.search_index = model.document if hasattr(model, "document") else model

    def list_search_indexes(self, name=None):
        self._polls += 1  # becomes queryable on the second poll, like an index that builds
        if not self.search_index:
            return []
        return [{"name": name, "queryable": self._polls > 1}]

    def find(self, query, projection=None):
        ids = query["_id"]["$in"]
        for i in ids:
            if i in self.docs:
                d = self.docs[i]
                yield {"_id": i, **{k: d[k] for k in (projection or {}) if k in d}}

    def bulk_write(self, ops, ordered=False):
        for op in ops:
            doc = op._doc
            self.docs[doc["_id"]] = copy.deepcopy(doc)

    def count_documents(self, query):
        return len(self.docs)

    def drop(self):
        self.db.colls.pop(self.name, None)

    def aggregate(self, pipeline):
        stage = pipeline[0]["$vectorSearch"]
        definition = self.search_index["definition"]
        vec_def = next(f for f in definition["fields"] if f["type"] == "vector")
        declared = {f["path"] for f in definition["fields"] if f["type"] == "filter"}
        flt = stage.get("filter", {})
        assert set(flt) <= declared, "filtering on a path the index does not declare"
        rows = []
        for d in self.docs.values():
            if all(d.get(k) == cond["$eq"] for k, cond in flt.items()):
                s = _sim(stage["queryVector"], d["vector"], vec_def["similarity"])
                rows.append({**d, "score": (1 + s) / 2})
        rows.sort(key=lambda r: -r["score"])
        rows = rows[: stage["limit"]]
        for st in pipeline[1:]:
            if "$match" in st:
                rows = [r for r in rows if all(r.get(k) == v for k, v in st["$match"].items())]
            if "$limit" in st:
                rows = rows[: st["$limit"]]
            if "$project" in st:
                keep = [k for k in st["$project"] if k != "score"]
                rows = [{"_id": r["_id"], "score": r["score"], **{k: r[k] for k in keep if k in r}} for r in rows]
        return iter(copy.deepcopy(rows))


class FakeMongoDB:
    def __init__(self) -> None:
        self.colls: dict[str, FakeMongoCollection] = {}

    def __getitem__(self, name):
        return self.colls.setdefault(name, FakeMongoCollection(self, name))

    def list_collection_names(self):
        return list(self.colls)

    def create_collection(self, name):
        self[name]


class FakeMongoClient:
    def __init__(self) -> None:
        self.dbs: dict[str, FakeMongoDB] = {}

    def __getitem__(self, name):
        return self.dbs.setdefault(name, FakeMongoDB())


# ---- Milvus -----------------------------------------------------------------------------------------
class FakeMilvusClient:
    def __init__(self) -> None:
        self.colls: dict[str, dict[str, Any]] = {}

    def has_collection(self, collection_name):
        return collection_name in self.colls

    def list_collections(self):
        return list(self.colls)

    def create_collection(self, collection_name, dimension, metric_type, id_type, max_length, enable_dynamic_field,
                          **kw):  # fmt: skip
        assert id_type == "string" and enable_dynamic_field
        self.colls[collection_name] = {"dim": dimension, "metric": metric_type, "rows": {}}

    def describe_collection(self, collection_name):
        c = self.colls[collection_name]
        return {"fields": [{"name": "id"}, {"name": "vector", "params": {"dim": c["dim"]}}]}

    def upsert(self, collection_name, data):
        c = self.colls[collection_name]
        for row in data:
            assert len(row["vector"]) == c["dim"]
            c["rows"][row["id"]] = copy.deepcopy(row)

    def get(self, collection_name, ids, output_fields):
        rows = self.colls[collection_name]["rows"]
        return [{"id": i, **{f: rows[i].get(f) for f in output_fields}} for i in ids if i in rows]

    @staticmethod
    def _match(row, expr):
        if not expr:
            return True
        for clause in expr.split(" and "):
            key, literal = clause.split(" == ", 1)
            if row.get(key) != json.loads(literal):
                return False
        return True

    def search(self, collection_name, data, limit, output_fields, search_params, filter=None, **kw):
        c = self.colls[collection_name]
        assert search_params["metric_type"] == c["metric"]
        hits = [
            {
                "id": i,
                "distance": _sim(data[0], r["vector"], c["metric"]),
                "entity": {f: r.get(f) for f in output_fields},
            }
            for i, r in c["rows"].items()
            if self._match(r, filter)
        ]
        hits.sort(key=lambda h: -h["distance"])
        return [hits[:limit]]

    def query(self, collection_name, filter, output_fields, **kw):
        assert output_fields == ["count(*)"]
        return [{"count(*)": len(self.colls[collection_name]["rows"])}]

    def drop_collection(self, collection_name):
        self.colls.pop(collection_name, None)


# ---- Weaviate ---------------------------------------------------------------------------------------
class _FakeWeaviateCollection:
    def __init__(self, description, metric):
        self.description, self.metric, self.objects = description, metric, {}
        self.config = SimpleNamespace(get=lambda: SimpleNamespace(description=self.description))
        self.data = SimpleNamespace(insert_many=self._insert_many)
        self.query = SimpleNamespace(near_vector=self._near_vector, fetch_objects_by_ids=self._fetch)
        self.aggregate = SimpleNamespace(over_all=lambda total_count: SimpleNamespace(total_count=len(self.objects)))

    def _insert_many(self, objects):
        for o in objects:
            self.objects[str(o.uuid)] = SimpleNamespace(uuid=o.uuid, properties=dict(o.properties),
                                                        vector={"default": list(o.vector)})  # fmt: skip
        return SimpleNamespace(has_errors=False, errors={})

    def _fetch(self, ids):
        return SimpleNamespace(objects=[self.objects[i] for i in ids if i in self.objects])

    @staticmethod
    def _ok(props, flt):
        if flt is None:
            return True
        conds = getattr(flt, "filters", None) or [flt]
        return all(props.get(c.target) == c.value for c in conds)

    def _near_vector(self, near_vector, limit, filters, return_metadata, include_vector):
        assert include_vector
        found = [o for o in self.objects.values() if self._ok(o.properties, filters)]
        found.sort(key=lambda o: -_sim(near_vector, o.vector["default"], self.metric))
        return SimpleNamespace(objects=found[:limit])


class FakeWeaviateClient:
    def __init__(self) -> None:
        self._colls: dict[str, _FakeWeaviateCollection] = {}
        outer = self

        class Collections:
            def exists(self, name):
                return name in outer._colls

            def create(self, name, description, vector_config, properties):
                metric = str(vector_config).lower()
                outer._colls[name] = _FakeWeaviateCollection(description, "dot" if "dot" in metric else "cosine")

            def use(self, name):
                return outer._colls[name]

            def delete(self, name):
                outer._colls.pop(name, None)

        self.collections = Collections()

    def close(self):
        pass


# ---- Redis (query engine) ------------------------------------------------------------------------
class _FakeSearch:
    def __init__(self, server: FakeRedis, name: str) -> None:
        self.server, self.name = server, name

    def create_index(self, fields, definition):
        vec = next(f for f in fields if type(f).__name__ == "VectorField")
        args = [a.decode() if isinstance(a, bytes) else str(a) for a in vec.args]
        dim = int(args[args.index("DIM") + 1])
        metric = args[args.index("DISTANCE_METRIC") + 1]
        prefix = definition.args[definition.args.index("PREFIX") + 2]
        self.server.indexes[self.name] = {"prefix": prefix, "dim": dim, "metric": metric,
                                          "fields": {f.name for f in fields}}  # fmt: skip

    def alter_schema_add(self, fields):
        self.server.indexes[self.name]["fields"] |= {f.name for f in fields}

    def info(self):
        idx = self.server.indexes[self.name]
        return {"num_docs": sum(1 for k in self.server.data if k.startswith(idx["prefix"]))}

    def dropindex(self, delete_documents):
        idx = self.server.indexes.pop(self.name)
        for k in [k for k in self.server.data if k.startswith(idx["prefix"])]:
            del self.server.data[k]

    def search(self, query, query_params):
        idx = self.server.indexes[self.name]
        text = query.query_string()
        m = re.match(r"^(\*|\((.*)\))=>\[KNN (\d+) @vector \$vec AS __score\]$", text)
        assert m, text
        clauses = re.findall(r"@(\w+):(\{(?:\\.|[^}])*\}|\[[^\]]*\])", m.group(2) or "")
        for field, _ in clauses:
            assert field in idx["fields"], f"field {field} not indexed"
        q = list(struct.unpack(f"<{idx['dim']}f", query_params["vec"]))
        rows = []
        for key, h in self.server.data.items():
            if not key.startswith(idx["prefix"]):
                continue
            ok = True
            for field, cond in clauses:
                value = h.get(field)
                if cond.startswith("{"):
                    ok &= value is not None and re.sub(r"\\(.)", r"\1", cond[1:-1]) == str(value)
                else:
                    lo = float(cond[1:-1].split()[0])
                    ok &= value is not None and float(value) == lo
            if ok:
                v = list(struct.unpack(f"<{idx['dim']}f", h["vector"]))
                s = _sim(q, v, idx["metric"])
                rows.append(SimpleNamespace(id=key, document=h["document"], embedding_model=h["embedding_model"],
                                            __score=str(1 - s)))  # fmt: skip
        rows.sort(key=lambda r: float(getattr(r, "__score")))
        docs = [
            _Doc(id=r.id, document=r.document, embedding_model=r.embedding_model, __score=getattr(r, "__score"))
            for r in rows[: int(m.group(3))]
        ]
        return SimpleNamespace(docs=docs)


class _Doc(dict):
    def __getattr__(self, item):
        return self.get(item)


class _FakePipeline:
    def __init__(self, server: FakeRedis) -> None:
        self.server, self.ops = server, []

    def __getattr__(self, name):
        def queue(*args, **kwargs):
            self.ops.append((name, args, kwargs))
            return self

        return queue

    def execute(self):
        return [getattr(self.server, name)(*a, **kw) for name, a, kw in self.ops]


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, dict[str, Any]] = {}
        self.indexes: dict[str, dict[str, Any]] = {}

    def ft(self, name):
        return _FakeSearch(self, name)

    def pipeline(self, transaction=False):
        return _FakePipeline(self)

    def hset(self, key, field=None, value=None, mapping=None):
        h = self.data.setdefault(key, {})
        if mapping:
            h.update(mapping)
        if field is not None:
            h[field] = value

    def hgetall(self, key):
        return {k.encode(): (v if isinstance(v, bytes) else str(v).encode()) for k, v in self.data.get(key, {}).items()}

    def hmget(self, key, fields):
        h = self.data.get(key)
        return [None if h is None or f not in h else str(h[f]).encode() for f in fields]

    def delete(self, key):
        self.data.pop(key, None)
