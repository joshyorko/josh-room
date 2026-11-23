"""Count request types from the task-owned MinIO audit webhook, discarding raw events."""

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from botocore.exceptions import ClientError


class MinioAudit:
    def __init__(self):
        self.condition = threading.Condition()
        self.writes = 0
        self.pending = None
        self.delivered = False
        audit = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("Content-Length", "0"))
                if size > 1024 * 1024:
                    self.send_error(413)
                    return
                try:
                    events = json.loads(self.rfile.read(size)) if size else []
                    for event in events if isinstance(events, list) else [events]:
                        api = event.get("api", {})
                        with audit.condition:
                            if not api.get("name", "").startswith(("Head", "Get", "List", "Health", "Stat")):
                                audit.writes += 1
                            if audit.pending and api.get("object") == audit.pending:
                                audit.delivered = True
                                audit.condition.notify_all()
                except (ValueError, AttributeError):
                    self.send_error(400)
                    return
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def checkpoint(self, client):
        key = "audit-check-" + uuid.uuid4().hex
        with self.condition:
            self.pending = key
            self.delivered = False
        try:
            client.head_object(Bucket="composition-fixture", Key=key)
        except ClientError as error:
            if error.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
                raise
        with self.condition:
            if not self.condition.wait_for(lambda: self.delivered, timeout=10):
                raise AssertionError("private MinIO audit did not acknowledge the checkpoint")
            return self.writes

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
