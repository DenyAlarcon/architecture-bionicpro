import json
import os
import urllib.error
import urllib.parse
import urllib.request
import datetime
import hashlib
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


CLICKHOUSE_URL = os.getenv("CLICKHOUSE_URL", "http://clickhouse:8123")
CLICKHOUSE_DB = os.getenv("CLICKHOUSE_DB", "reporting")
REPORTS_TABLE = os.getenv("REPORTS_TABLE", "user_report_mart")
S3_ENDPOINT = os.getenv("S3_ENDPOINT", "http://minio:9000")
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "minioadmin")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "minioadmin")
S3_BUCKET = os.getenv("S3_BUCKET", "reports")
S3_REGION = os.getenv("S3_REGION", "us-east-1")
CDN_PUBLIC_BASE_URL = os.getenv("CDN_PUBLIC_BASE_URL", "http://localhost:8083/reports")


def clickhouse_query(sql: str) -> list[dict]:
    params = urllib.parse.urlencode({"database": CLICKHOUSE_DB, "query": sql})
    url = f"{CLICKHOUSE_URL.rstrip('/')}/?{params}"
    request = urllib.request.Request(url)
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8").strip()
    if not body:
        return []
    return [json.loads(line) for line in body.splitlines()]


def report_for_user(user_id: str) -> dict | None:
    quoted_user = user_id.replace("\\", "\\\\").replace("'", "\\'")
    table_name = REPORTS_TABLE.replace("\\", "\\\\").replace("`", "")
    rows = clickhouse_query(
        f"""
        SELECT
            user_id,
            client_name,
            report_date,
            processed_until,
            events_count,
            avg_battery_level,
            avg_signal_quality,
            max_temperature,
            total_active_minutes
        FROM `{table_name}` FINAL
        WHERE user_id = '{quoted_user}'
        ORDER BY report_date DESC
        LIMIT 1
        FORMAT JSONEachRow
        """
    )
    if not rows:
        return None
    return rows[0]


def safe_object_part(value: str) -> str:
    return urllib.parse.quote(value, safe="-_.@")


def report_object_key(user_id: str) -> str:
    report_day = datetime.datetime.now(datetime.UTC).date().isoformat()
    return f"daily/{report_day}/{safe_object_part(REPORTS_TABLE)}/{safe_object_part(user_id)}.json"


def cdn_url(object_key: str) -> str:
    return f"{CDN_PUBLIC_BASE_URL.rstrip('/')}/{object_key}"


def sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def signing_key(date_stamp: str) -> bytes:
    date_key = sign(("AWS4" + S3_SECRET_KEY).encode("utf-8"), date_stamp)
    region_key = sign(date_key, S3_REGION)
    service_key = sign(region_key, "s3")
    return sign(service_key, "aws4_request")


def s3_request(method: str, path: str, body: bytes = b"", content_type: str | None = None) -> bytes:
    parsed = urllib.parse.urlparse(S3_ENDPOINT.rstrip("/"))
    host = parsed.netloc
    target = urllib.parse.urlsplit(path)
    now = datetime.datetime.now(datetime.UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()

    canonical_uri = urllib.parse.quote(target.path, safe="/~")
    canonical_query = target.query
    if canonical_query and "=" not in canonical_query:
        canonical_query = f"{canonical_query}="
    canonical_headers = (
        f"host:{host}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join(
        [method, canonical_uri, canonical_query, canonical_headers, signed_headers, payload_hash]
    )
    credential_scope = f"{date_stamp}/{S3_REGION}/s3/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signature = hmac.new(
        signing_key(date_stamp),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "Authorization": (
            "AWS4-HMAC-SHA256 "
            f"Credential={S3_ACCESS_KEY}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
        "x-amz-content-sha256": payload_hash,
        "x-amz-date": amz_date,
    }
    if content_type:
        headers["Content-Type"] = content_type

    url = f"{S3_ENDPOINT.rstrip('/')}{path}"
    request = urllib.request.Request(url, data=body if method != "HEAD" else None, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read()


def s3_object_exists(object_key: str) -> bool:
    try:
        s3_request("HEAD", f"/{S3_BUCKET}/{object_key}")
        return True
    except urllib.error.HTTPError as error:
        if error.code == HTTPStatus.NOT_FOUND:
            return False
        raise


def put_s3_object(object_key: str, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    s3_request(
        "PUT",
        f"/{S3_BUCKET}/{object_key}",
        body,
        "application/json; charset=utf-8",
    )


def ensure_bucket() -> None:
    try:
        s3_request("PUT", f"/{S3_BUCKET}")
    except urllib.error.HTTPError as error:
        if error.code not in (HTTPStatus.CONFLICT, HTTPStatus.FORBIDDEN):
            raise

    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": ["*"]},
                "Action": ["s3:GetObject"],
                "Resource": [f"arn:aws:s3:::{S3_BUCKET}/*"],
            }
        ],
    }
    body = json.dumps(policy).encode("utf-8")
    s3_request("PUT", f"/{S3_BUCKET}?policy", body, "application/json")


class Handler(BaseHTTPRequestHandler):
    server_version = "bionicpro-reports/1.0"

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            self.json_response({"status": "ok"})
            return
        if parsed.path != "/reports":
            self.json_response({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return

        query = urllib.parse.parse_qs(parsed.query)
        requested_user = query.get("user_id", [""])[0]
        authenticated_user = self.headers.get("X-User-Id", "")

        if not authenticated_user:
            self.json_response({"error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
            return
        if not requested_user or requested_user != authenticated_user:
            self.json_response({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            return

        object_key = report_object_key(authenticated_user)
        try:
            ensure_bucket()
            if s3_object_exists(object_key):
                self.json_response(
                    {
                        "cdn_url": cdn_url(object_key),
                        "object_key": object_key,
                        "cached": True,
                    }
                )
                return
            report = report_for_user(authenticated_user)
        except (urllib.error.URLError, TimeoutError):
            self.json_response({"error": "storage_or_olap_unavailable"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return

        if not report:
            self.json_response({"error": "report_not_ready"}, HTTPStatus.NOT_FOUND)
            return

        stored_report = {"report": report}
        try:
            put_s3_object(object_key, stored_report)
        except (urllib.error.URLError, TimeoutError):
            self.json_response({"error": "storage_unavailable"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return

        self.json_response(
            {
                "cdn_url": cdn_url(object_key),
                "object_key": object_key,
                "cached": False,
                "report": report,
            }
        )

    def json_response(self, payload: dict, status: HTTPStatus = HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    port = int(os.getenv("PORT", "8010"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"bionicpro-reports listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
