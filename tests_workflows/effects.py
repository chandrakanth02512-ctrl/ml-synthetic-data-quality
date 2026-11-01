"""Fake REST responses; all orchestration decisions remain in the actual YAML."""

import copy
import json
from urllib.parse import unquote, urlsplit

from google.api_core.exceptions import PreconditionFailed

from synthetic_quality_lakehouse.config import canonical, gcs_parts
from tests_workflows.engine import WorkflowError


class Interrupted(BaseException):
    pass


class Effects:
    def __init__(self, store, compiled):
        self.store, self.p = store, compiled
        self.jobs, self.submissions = {}, []
        self.states, self.hooks, self.errors = {}, {}, {}
        self.lost_ack = None
        self.interrupt_at = None
        self.duplicate_vertex = False
        self.page_vertex = False
        self.inventory_missing = False

    def error(self, code):
        raise WorkflowError({"code": code, "tags": ["HttpError"], "message": "redacted API error"})

    def __call__(self, method, args):
        url, query = args["url"], args.get("query", {})
        if self.errors.get(url):
            self.error(self.errors[url].pop(0))
        if "storage.googleapis.com" in url:
            return self.storage(method, args)
        if method == "http.post":
            stage = next(
                s for s in self.p["stages"] if s["collection"] == url and s["query"] == query
            )
            self.submissions.append(stage["id"])
            name = (
                stage["name"]
                or "projects/gcp-intelligent-data-quality/locations/asia-south1/customJobs/123"
            )
            job = copy.deepcopy(args["body"])
            job.update(
                name=name,
                state="SUCCEEDED" if stage["kind"] == "dataproc" else "JOB_STATE_SUCCEEDED",
            )
            self.jobs[name] = job
            if stage["id"] in self.hooks:
                self.hooks[stage["id"]]()
            else:
                bucket, path = self.object_parts(stage["result_url"])
                report = {"status": "COMPLETE"}
                if stage["kind"] == "vertex":
                    report.update(
                        run_id=self.p["run_id"],
                        plan_hash=self.p["plan_hash"],
                        artifacts_verified=True,
                        reconciled=True,
                        verified_objects=[self.p["reference"]] * 17,
                        format_version=1,
                        training_rows=self.p["training_rows"],
                        evaluation_rows=self.p["evaluation_rows"],
                        model_sha256="a" * 64,
                        publications={
                            k: {"status": "PUBLISHED"}
                            for k in ("training", "evaluation", "predictions")
                        },
                    )
                self.store.create(f"gs://{bucket}/{path}", canonical(report))
            if self.lost_ack == stage["id"]:
                self.lost_ack = None
                raise WorkflowError({"tags": ["TimeoutError"], "message": "lost create response"})
            return {"body": job, "code": 200}
        stage = next((s for s in self.p["stages"] if s["collection"] == url), None)
        if stage:
            jobs = [j for j in self.jobs.values() if "/customJobs/" in j["name"]]
            if self.inventory_missing:
                jobs = []
            if self.duplicate_vertex and jobs:
                jobs = jobs + [copy.deepcopy(jobs[0])]
            if self.page_vertex and jobs and not query.get("pageToken"):
                return {"body": {"customJobs": [], "nextPageToken": "page2"}, "code": 200}
            return {"body": {"customJobs": copy.deepcopy(jobs)}, "code": 200}
        name = url.split("/v1/", 1)[1]
        if name not in self.jobs:
            self.error(404)
        job = copy.deepcopy(self.jobs[name])
        key = next(
            s["id"]
            for s in self.p["stages"]
            if name == s["name"] or s["kind"] == "vertex" and "/customJobs/" in name
        )
        job["state"] = self.states.get(key, job["state"])
        return {"body": job, "code": 200}

    @staticmethod
    def object_parts(url):
        path = urlsplit(url).path
        return unquote(path.split("/b/", 1)[1].split("/o", 1)[0]), unquote(path.split("/o/", 1)[1])

    def storage(self, method, args):
        query = args.get("query", {})
        if method == "http.post":
            assert query["ifGenerationMatch"] == 0 and query["uploadType"] == "media"
            bucket = args["url"].split("/b/", 1)[1].split("/o", 1)[0]
            name = query["name"]
            blob = self.store.client.bucket(bucket).blob(name)
            try:
                blob.upload_from_string(canonical(args["body"]), if_generation_match=0, retry=None)
            except PreconditionFailed:
                self.error(412)
            if self.interrupt_at and name.endswith(self.interrupt_at):
                self.interrupt_at = None
                raise Interrupted()
            if self.lost_ack and name.endswith(self.lost_ack):
                self.lost_ack = None
                raise WorkflowError({"tags": ["TimeoutError"]})
            return {
                "body": {"generation": str(self.store.read(f"gs://{bucket}/{name}")[1])},
                "code": 200,
            }
        bucket, name = self.object_parts(args["url"])
        try:
            data, generation = self.store.read(
                f"gs://{bucket}/{name}", int(query["generation"]) if "generation" in query else None
            )
        except FileNotFoundError:
            self.error(404)
        if query.get("alt") == "media":
            try:
                body = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                body = data  # application/octet-stream and Python source are bytes in this fake.
            return {"body": body, "code": 200}
        return {"body": {"generation": str(generation), "size": str(len(data))}, "code": 200}

    def corrupt(self, url, change):
        bucket, path = self.object_parts(url)
        data, generation = self.store.client.bucket(bucket).objects[path]
        value = json.loads(data)
        change(value)
        self.store.client.bucket(bucket).objects[path] = canonical(value), generation

    def marker(self, stage):
        uri = f"gs://{self.p['bucket']}/{self.p['prefix']}/{stage}/attempt.json"
        return gcs_parts(uri)
