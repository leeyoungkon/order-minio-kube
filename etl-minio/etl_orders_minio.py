"""학생 실습 1단계: MySQL 주문 변경을 10초마다 확인하여 MinIO에 Parquet 저장.

필요 패키지: python -m pip install 'PyMySQL[rsa]' boto3 pyarrow
실행 위치: Kubernetes Pod (MySQL/MinIO Service DNS로 접속).
실행 명령: python -u etl_orders_minio.py

필수 환경변수:
  MYSQL_PASSWORD, MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY
선택 환경변수:
  MYSQL_HOST=mysql, MYSQL_PORT=3306, MYSQL_USER=root,
  MYSQL_DATABASE=orderdb, MYSQL_TABLE=orders,
  MINIO_BUCKET=warehouse, MINIO_PREFIX=raw/orders_step1/,
  CREATE_BUCKET=true

전제: order_id가 고유한 orders 테이블. 아래 SCHEMA의 주문 열 사용.
simulation_phase 열은 없거나 NULL이어도 됨. 나머지 주문 열은 필요함.
최초 실행은 기존 주문도 저장. 이후 신규/수정 주문의 현재 행을 저장.
한 번에 여러 주문을 한 파일로 묶음. 변경 없으면 파일을 만들지 않음.
삭제 이벤트와 10초 사이에 발생했다가 사라지는 중간 변경은 수집하지 않음.
실습용으로 매번 전체 주문 테이블을 읽음. 대규모 운영에는 CDC 등 필요.
Pod replicas=1 사용. 처리 이력은 MinIO checkpoint에 보존.
실패 시 완료 이력을 갱신하지 않고 재시도. Pod 재시작이 업로드와
checkpoint 기록 사이에 발생하면 같은 주문 버전이 여러 파일에 남을 수
있음(at-least-once). 이후 분석은 order_id별 etl_extracted_at 최신 행 사용.
checkpoint 파일을 지우면 전체 주문이 다시 수집됨.
order_time은 DB 원본 시각을 그대로 저장, etl_extracted_at은 UTC로 저장.
"""

import hashlib
import json
import logging
import os
import re
import signal
import threading
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from io import BytesIO

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
import pymysql
from botocore.config import Config
from botocore.exceptions import ClientError

LOG = logging.getLogger("orders-etl")
POLL_SECONDS = 10
SCHEMA = pa.schema([
    ("order_id", pa.string()), ("customer_id", pa.string()),
    ("product_id", pa.string()), ("quantity", pa.int64()),
    ("unit_price", pa.decimal128(20, 2)),
    ("total_amount", pa.decimal128(20, 2)),
    ("simulation_phase", pa.string()), ("order_time", pa.timestamp("us")),
    ("etl_change_type", pa.string()),
    ("etl_extracted_at", pa.timestamp("us", tz="UTC")),
])


def json_value(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"지원하지 않는 DB 값 형식: {type(value).__name__}")


def fingerprint(row):
    # 행의 내용이 같으면 같은 해시: 주문번호와 시각이 작아도 신규 주문 감지.
    canonical = json.dumps(row, default=json_value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def make_parquet(changes, extracted_at):
    records = []
    for row, change_type in changes:
        record = dict(row)
        for field in ("order_id", "customer_id", "product_id"):
            if record.get(field) is None or not str(record[field]).strip():
                raise ValueError(f"{field}가 없는 주문입니다.")
            record[field] = str(record[field])
        quantity = Decimal(str(record["quantity"]))
        if not quantity.is_finite() or quantity <= 0 or quantity != quantity.to_integral_value():
            raise ValueError("quantity는 양의 정수여야 합니다.")
        record["quantity"] = int(quantity)
        for field in ("unit_price", "total_amount"):
            money = Decimal(str(record[field]))
            if not money.is_finite() or money < 0:
                raise ValueError(f"{field}는 0 이상의 금액이어야 합니다.")
            record[field] = money
        if not isinstance(record.get("order_time"), datetime):
            raise ValueError("order_time은 MySQL DATETIME/TIMESTAMP여야 합니다.")
        phase = record.get("simulation_phase")
        record["simulation_phase"] = None if phase is None else str(phase)
        record.update(etl_change_type=change_type, etl_extracted_at=extracted_at)
        records.append(record)
    # Decimal 금액을 float로 바꾸지 않으며 스키마를 배치마다 고정합니다.
    buffer = BytesIO()
    pq.write_table(pa.Table.from_pylist(records, schema=SCHEMA), buffer, compression="snappy")
    return buffer.getvalue()


class OrderETL:
    def __init__(self):
        self.table = os.getenv("MYSQL_TABLE", "orders")
        if not re.fullmatch(r"[A-Za-z0-9_]+", self.table):
            raise ValueError("MYSQL_TABLE에는 영문, 숫자, 밑줄만 사용할 수 있습니다.")
        self.db = dict(
            host=os.getenv("MYSQL_HOST", "mysql"), port=int(os.getenv("MYSQL_PORT", "3306")),
            user=os.getenv("MYSQL_USER", "root"), password=os.environ["MYSQL_PASSWORD"],
            database=os.getenv("MYSQL_DATABASE", "orderdb"), charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor, autocommit=True,
            connect_timeout=5, read_timeout=30, write_timeout=30,
        )
        self.bucket = os.getenv("MINIO_BUCKET", "warehouse")
        self.prefix = os.getenv("MINIO_PREFIX", "raw/orders_step1/").strip("/") + "/"
        if self.prefix == "/":
            raise ValueError("MINIO_PREFIX를 지정하세요.")
        self.checkpoint_key = self.prefix + "_checkpoint/state.json"
        self.source = f"{self.db['host']}:{self.db['port']}/{self.db['database']}/{self.table}"
        self.s3 = boto3.client(
            "s3", endpoint_url=os.environ["MINIO_ENDPOINT"],
            aws_access_key_id=os.environ["MINIO_ACCESS_KEY"],
            aws_secret_access_key=os.environ["MINIO_SECRET_KEY"], region_name="us-east-1",
            verify=False,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                          connect_timeout=5, read_timeout=30,
                          retries={"mode": "standard", "total_max_attempts": 2},
                          request_checksum_calculation="when_required",
                          response_checksum_validation="when_required"),
        )
        self.previous = None  # None은 checkpoint를 아직 읽지 않았다는 뜻.
        self.pending = None   # 업로드 실패 시 같은 파일/내용을 다음 주기에 재시도.

    def initialize(self):
        try:
            self.s3.head_bucket(Bucket=self.bucket)
        except ClientError as exc:
            code = str(exc.response["Error"]["Code"])
            if code not in {"404", "NoSuchBucket", "NotFound"} or os.getenv("CREATE_BUCKET", "true").lower() != "true":
                raise
            self.s3.create_bucket(Bucket=self.bucket)
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=self.checkpoint_key)
            try:
                state = json.loads(response["Body"].read())
            finally:
                response["Body"].close()
            if state["source"] != self.source:
                raise ValueError("기존 checkpoint의 DB가 다릅니다. 새 MINIO_PREFIX를 사용하세요.")
            hashes = state["hashes"]
            if not isinstance(hashes, dict):
                raise ValueError("checkpoint 형식이 잘못되었습니다.")
            self.previous = hashes
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in {"NoSuchKey", "404"}:
                raise  # 접근 오류를 이력이 없는 것으로 간주하지 않음.
            self.previous = {}

    def read_orders(self):
        connection = pymysql.connect(**self.db)
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"SELECT * FROM `{self.table}` ORDER BY order_id")
                return cursor.fetchall()
        finally:
            connection.close()

    def prepare_batch(self, rows):
        changes, seen = [], set()
        new_hashes = dict(self.previous)
        for row in rows:
            if row.get("order_id") is None:
                raise ValueError("order_id가 없는 주문입니다.")
            order_id = str(row["order_id"])
            if order_id in seen:
                raise ValueError("order_id는 주문마다 고유해야 합니다.")
            seen.add(order_id)
            digest = fingerprint(row)
            if self.previous.get(order_id) != digest:
                operation = "insert" if order_id not in self.previous else "update"
                changes.append((row, operation))
                new_hashes[order_id] = digest
        if not changes:
            return None
        now = datetime.now(timezone.utc)
        key = f"{self.prefix}date={now:%Y-%m-%d}/orders_{now:%H%M%S}_{uuid.uuid4().hex}.parquet"
        return dict(key=key, payload=make_parquet(changes, now), hashes=new_hashes,
                    inserts=sum(operation == "insert" for _, operation in changes),
                    updates=sum(operation == "update" for _, operation in changes))

    def run_once(self):
        if self.previous is None:
            self.initialize()
        if self.pending is None:
            self.pending = self.prepare_batch(self.read_orders())
        if self.pending is None:
            LOG.info("주문 변화 없음")
            return 0
        batch = self.pending
        # 반드시 데이터 업로드 → 처리 이력 저장 순서. 실패하면 pending 유지.
        self.s3.put_object(Bucket=self.bucket, Key=batch["key"], Body=batch["payload"],
                           ContentType="application/vnd.apache.parquet")
        state = json.dumps({"source": self.source, "hashes": batch["hashes"]},
                           ensure_ascii=False).encode("utf-8")
        self.s3.put_object(Bucket=self.bucket, Key=self.checkpoint_key, Body=state,
                           ContentType="application/json")
        self.previous = batch["hashes"]
        self.pending = None
        count = batch["inserts"] + batch["updates"]
        LOG.info("저장 성공: 신규=%s 수정=%s → s3://%s/%s",
                 batch["inserts"], batch["updates"], self.bucket, batch["key"])
        return count


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    etl = OrderETL()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        try:
            etl.run_once()
        except Exception:
            LOG.exception("ETL 실패: 처리 이력을 유지하고 10초 후 재시도합니다.")
        # 정상 처리/실패 후 10초 대기. 처리 시간만큼 실제 시작 간격은 늘어남.
        stop.wait(POLL_SECONDS)


if __name__ == "__main__":
    main()
