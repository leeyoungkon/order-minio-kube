import os
import time
from datetime import datetime

import pymysql


# ======================================================
# DB 설정
# ======================================================

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "root1234")

SOURCE_DB = os.getenv("SOURCE_DB", "orderdb")
TARGET_DB = os.getenv("TARGET_DB", "analytics")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "30"))


# ======================================================
# DB 연결
# ======================================================

def connect_db():
    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        charset="utf8mb4",
        autocommit=False
    )


# ======================================================
# 분석 DB 및 테이블 생성
# ======================================================

def initialize_database(conn):

    with conn.cursor() as cursor:

        cursor.execute(
            f"CREATE DATABASE IF NOT EXISTS {TARGET_DB}"
        )

        cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {TARGET_DB}.customer_order_minute (

            minute_time DATETIME NOT NULL,

            customer_id VARCHAR(10) NOT NULL,

            order_count INT NOT NULL,

            total_quantity INT NOT NULL,

            total_amount BIGINT NOT NULL,

            PRIMARY KEY (
                minute_time,
                customer_id
            )
        )
        """)

        cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS {TARGET_DB}.etl_status (

            job_name VARCHAR(50) PRIMARY KEY,

            last_processed_time DATETIME NULL,

            last_run_time DATETIME NULL,

            last_status VARCHAR(20),

            processed_rows INT DEFAULT 0,

            error_message VARCHAR(500)
        )
        """)

    conn.commit()


# ======================================================
# ETL 상태 조회
# ======================================================

def get_last_processed_time(conn):

    with conn.cursor() as cursor:

        cursor.execute(f"""
        SELECT last_processed_time
        FROM {TARGET_DB}.etl_status
        WHERE job_name = 'order_etl'
        """)

        result = cursor.fetchone()

        if result is None:
            return None

        return result[0]


# ======================================================
# 신규 주문 존재 여부 확인
# ======================================================

def check_new_orders(conn, last_processed_time):

    with conn.cursor() as cursor:

        if last_processed_time is None:

            cursor.execute(f"""
            SELECT COUNT(*)
            FROM {SOURCE_DB}.orders
            """)

        else:

            cursor.execute(f"""
            SELECT COUNT(*)
            FROM {SOURCE_DB}.orders
            WHERE order_time > %s
            """, (last_processed_time,))

        count = cursor.fetchone()[0]

        return count


# ======================================================
# 신규 데이터 범위 확인
# ======================================================

def get_new_order_time_range(conn, last_processed_time):

    with conn.cursor() as cursor:

        if last_processed_time is None:

            cursor.execute(f"""
            SELECT
                MIN(order_time),
                MAX(order_time)
            FROM {SOURCE_DB}.orders
            """)

        else:

            cursor.execute(f"""
            SELECT
                MIN(order_time),
                MAX(order_time)
            FROM {SOURCE_DB}.orders
            WHERE order_time > %s
            """, (last_processed_time,))

        return cursor.fetchone()


# ======================================================
# ETL 실행
# ======================================================

def run_etl(conn, last_processed_time):

    print("[Agent] ETL 실행 시작")

    with conn.cursor() as cursor:

        if last_processed_time is None:

            sql = f"""
            INSERT INTO {TARGET_DB}.customer_order_minute
            (
                minute_time,
                customer_id,
                order_count,
                total_quantity,
                total_amount
            )

            SELECT
                TIMESTAMP(
                    DATE(order_time),
                    MAKETIME(
                        HOUR(order_time),
                        MINUTE(order_time),
                        0
                    )
                ) AS minute_time,

                customer_id,

                COUNT(*) AS order_count,
                SUM(quantity) AS total_quantity,
                SUM(total_amount) AS total_amount

            FROM {SOURCE_DB}.orders

            GROUP BY
                TIMESTAMP(
                    DATE(order_time),
                    MAKETIME(
                        HOUR(order_time),
                        MINUTE(order_time),
                        0
                    )
                ),
                customer_id

            ON DUPLICATE KEY UPDATE

                order_count = VALUES(order_count),
                total_quantity = VALUES(total_quantity),
                total_amount = VALUES(total_amount)
            """

            cursor.execute(sql)

        else:

            sql = f"""
            INSERT INTO {TARGET_DB}.customer_order_minute
            (
                minute_time,
                customer_id,
                order_count,
                total_quantity,
                total_amount
            )

            SELECT
                TIMESTAMP(
                    DATE(order_time),
                    MAKETIME(
                        HOUR(order_time),
                        MINUTE(order_time),
                        0
                    )
                ) AS minute_time,

                customer_id,

                COUNT(*) AS order_count,
                SUM(quantity) AS total_quantity,
                SUM(total_amount) AS total_amount

            FROM {SOURCE_DB}.orders

            WHERE order_time > %s

            GROUP BY
                TIMESTAMP(
                    DATE(order_time),
                    MAKETIME(
                        HOUR(order_time),
                        MINUTE(order_time),
                        0
                    )
                ),
                customer_id

            ON DUPLICATE KEY UPDATE

                order_count =
                    order_count + VALUES(order_count),

                total_quantity =
                    total_quantity + VALUES(total_quantity),

                total_amount =
                    total_amount + VALUES(total_amount)
            """

            cursor.execute(
                sql,
                (last_processed_time,)
            )

        affected_rows = cursor.rowcount

    conn.commit()

    return affected_rows


# ======================================================
# 데이터 품질 검사
# ======================================================

def validate_data(conn):

    with conn.cursor() as cursor:

        cursor.execute(f"""
        SELECT COUNT(*)
        FROM {TARGET_DB}.customer_order_minute
        WHERE
            total_quantity < 0
            OR total_amount < 0
            OR order_count <= 0
        """)

        invalid_count = cursor.fetchone()[0]

    return invalid_count == 0


# ======================================================
# ETL 상태 저장
# ======================================================

def update_etl_status(
    conn,
    status,
    processed_rows,
    last_processed_time,
    error_message=None
):

    with conn.cursor() as cursor:

        sql = f"""
        INSERT INTO {TARGET_DB}.etl_status
        (
            job_name,
            last_processed_time,
            last_run_time,
            last_status,
            processed_rows,
            error_message
        )

        VALUES
        (
            'order_etl',
            %s,
            %s,
            %s,
            %s,
            %s
        )

        ON DUPLICATE KEY UPDATE

            last_processed_time =
                VALUES(last_processed_time),

            last_run_time =
                VALUES(last_run_time),

            last_status =
                VALUES(last_status),

            processed_rows =
                VALUES(processed_rows),

            error_message =
                VALUES(error_message)
        """

        cursor.execute(
            sql,
            (
                last_processed_time,
                datetime.now(),
                status,
                processed_rows,
                error_message
            )
        )

    conn.commit()


# ======================================================
# Agent 판단 로직
# ======================================================

def etl_agent():

    conn = None

    try:

        conn = connect_db()

        initialize_database(conn)

        last_processed_time = (
            get_last_processed_time(conn)
        )

        print(
            "[Agent] 마지막 처리 시각:",
            last_processed_time
        )


        # ------------------------------------------------
        # 1. 신규 주문 확인
        # ------------------------------------------------

        new_order_count = check_new_orders(
            conn,
            last_processed_time
        )

        print(
            "[Agent] 신규 주문 수:",
            new_order_count
        )


        # ------------------------------------------------
        # 2. Agent 판단
        # ------------------------------------------------

        if new_order_count == 0:

            print(
                "[Agent Decision] "
                "신규 주문 없음 → ETL 실행하지 않음"
            )

            return


        print(
            "[Agent Decision] "
            "신규 주문 발견 → ETL 실행"
        )


        # ------------------------------------------------
        # 3. 처리 범위 확인
        # ------------------------------------------------

        min_time, max_time = (
            get_new_order_time_range(
                conn,
                last_processed_time
            )
        )

        print(
            "[Agent] 처리 범위:",
            min_time,
            "~",
            max_time
        )


        # ------------------------------------------------
        # 4. ETL 실행
        # ------------------------------------------------

        processed_rows = run_etl(
            conn,
            last_processed_time
        )


        # ------------------------------------------------
        # 5. 품질 검사
        # ------------------------------------------------

        valid = validate_data(conn)

        if not valid:

            print(
                "[Agent Decision] "
                "데이터 품질 이상 발견"
            )

            update_etl_status(
                conn,
                "FAILED",
                processed_rows,
                last_processed_time,
                "Data quality validation failed"
            )

            return


        # ------------------------------------------------
        # 6. 상태 갱신
        # ------------------------------------------------

        update_etl_status(
            conn,
            "SUCCESS",
            processed_rows,
            max_time
        )


        print(
            "[Agent Decision] ETL 성공"
        )

        print(
            "[Agent] 처리된 집계 행:",
            processed_rows
        )

        print(
            "[Agent] 새로운 처리 기준 시각:",
            max_time
        )


    except Exception as e:

        print(
            "[Agent Error]",
            repr(e)
        )

        if conn:

            try:

                conn.rollback()

                update_etl_status(
                    conn,
                    "FAILED",
                    0,
                    None,
                    str(e)
                )

            except Exception:
                pass

    finally:

        if conn:
            conn.close()


# ======================================================
# Agent 실행 Loop
# ======================================================

def main():

    print("==============================")
    print(" ETL Agent Started")
    print("==============================")

    while True:

        print()
        print(
            "[Agent] 신규 주문 확인..."
        )

        etl_agent()

        print(
            f"[Agent] {CHECK_INTERVAL}초 대기"
        )

        time.sleep(
            CHECK_INTERVAL
        )


if __name__ == "__main__":
    main()