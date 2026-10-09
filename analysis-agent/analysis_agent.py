import os
import io
import base64
import html

import pymysql
import pandas as pd

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse


# ======================================================
# 환경 설정
# ======================================================

DB_HOST = os.getenv("DB_HOST", "mysql")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_NAME = os.getenv("DB_NAME", "analytics")
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "root1234")

app = FastAPI(
    title="Customer Analysis Agent"
)


# ======================================================
# DB 연결
# ======================================================

def connect_db():

    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor
    )


# ======================================================
# 최신 데이터 시간
#
# NOW() 대신 실제 DB에 저장된 가장 최근 시각을 사용한다.
# Simulator가 잠시 중단되어도 분석 가능.
# ======================================================

def get_latest_time(conn):

    sql = """
    SELECT MAX(minute_time) AS latest_time
    FROM customer_order_minute
    """

    with conn.cursor() as cursor:

        cursor.execute(sql)

        row = cursor.fetchone()

    if not row:
        return None

    return row["latest_time"]


# ======================================================
# Tool 1
# 고객별 주문량 추이
# ======================================================

def get_customer_trend(conn, minutes=10):

    latest_time = get_latest_time(conn)

    if latest_time is None:
        return pd.DataFrame()

    sql = f"""
    SELECT
        minute_time,
        customer_id,
        total_quantity
    FROM customer_order_minute
    WHERE
        minute_time >=
        DATE_SUB(%s, INTERVAL {int(minutes)} MINUTE)
    ORDER BY
        minute_time,
        customer_id
    """

    with conn.cursor() as cursor:

        cursor.execute(
            sql,
            (latest_time,)
        )

        rows = cursor.fetchall()

    return pd.DataFrame(rows)


# ======================================================
# Tool 2
# 고객별 증가/감소 분석
#
# 최근 5분과 이전 5분 비교
# ======================================================

def get_customer_growth(conn, minutes=5):

    latest_time = get_latest_time(conn)

    if latest_time is None:
        return pd.DataFrame()

    total_minutes = minutes * 2

    sql = f"""
    SELECT
        minute_time,
        customer_id,
        total_quantity
    FROM customer_order_minute
    WHERE
        minute_time >=
        DATE_SUB(%s, INTERVAL {int(total_minutes)} MINUTE)
    ORDER BY
        minute_time,
        customer_id
    """

    with conn.cursor() as cursor:

        cursor.execute(
            sql,
            (latest_time,)
        )

        rows = cursor.fetchall()

    df = pd.DataFrame(rows)

    if df.empty:
        return df

    df["minute_time"] = pd.to_datetime(
        df["minute_time"]
    )

    latest_time = pd.Timestamp(
        latest_time
    )

    recent_start = (
        latest_time
        - pd.Timedelta(minutes=minutes)
    )

    previous_start = (
        latest_time
        - pd.Timedelta(minutes=minutes * 2)
    )

    recent_df = df[
        df["minute_time"] > recent_start
    ]

    previous_df = df[
        (df["minute_time"] > previous_start)
        &
        (df["minute_time"] <= recent_start)
    ]


    recent_sum = (
        recent_df
        .groupby("customer_id")["total_quantity"]
        .sum()
        .rename("recent_quantity")
    )


    previous_sum = (
        previous_df
        .groupby("customer_id")["total_quantity"]
        .sum()
        .rename("previous_quantity")
    )


    result = pd.concat(
        [
            recent_sum,
            previous_sum
        ],
        axis=1
    ).fillna(0)


    result["growth"] = (
        result["recent_quantity"]
        -
        result["previous_quantity"]
    )


    def growth_rate(row):

        previous = row[
            "previous_quantity"
        ]

        if previous == 0:
            return 0

        return (
            row["growth"]
            / previous
        ) * 100


    result["growth_rate"] = (
        result.apply(
            growth_rate,
            axis=1
        )
    )


    result = (
        result
        .reset_index()
        .sort_values(
            "growth",
            ascending=False
        )
    )

    return result


# ======================================================
# Tool 3
# 고객별 매출 분석
# ======================================================

def get_customer_sales(conn, minutes=10):

    latest_time = get_latest_time(conn)

    if latest_time is None:
        return pd.DataFrame()

    sql = f"""
    SELECT
        customer_id,
        SUM(total_amount) AS total_sales,
        SUM(total_quantity) AS total_quantity,
        SUM(order_count) AS order_count
    FROM customer_order_minute
    WHERE
        minute_time >=
        DATE_SUB(%s, INTERVAL {int(minutes)} MINUTE)
    GROUP BY
        customer_id
    ORDER BY
        total_sales DESC
    """

    with conn.cursor() as cursor:

        cursor.execute(
            sql,
            (latest_time,)
        )

        rows = cursor.fetchall()

    return pd.DataFrame(rows)


# ======================================================
# Agent 판단
# ======================================================

def decide_action(question):

    question = question.lower()

    if (
        "증가" in question
        or "감소" in question
        or "성장" in question
        or "변화량" in question
    ):
        return "growth"

    if (
        "매출" in question
        or "금액" in question
        or "매상" in question
    ):
        return "sales"

    if (
        "추이" in question
        or "트렌드" in question
        or "주문량" in question
        or "그래프" in question
    ):
        return "trend"

    return "trend"


# ======================================================
# 그래프 → Base64 변환
#
# Kubernetes에서 파일 저장 필요 없음
# ======================================================

def figure_to_base64(fig):

    buffer = io.BytesIO()

    fig.savefig(
        buffer,
        format="png",
        dpi=140,
        bbox_inches="tight"
    )

    plt.close(fig)

    buffer.seek(0)

    encoded = base64.b64encode(
        buffer.read()
    ).decode("utf-8")

    return encoded


# ======================================================
# Trend 그래프
# ======================================================

def create_trend_chart(df):

    pivot = df.pivot_table(
        index="minute_time",
        columns="customer_id",
        values="total_quantity",
        aggfunc="sum",
        fill_value=0
    )

    fig, ax = plt.subplots(
        figsize=(10, 5)
    )

    for customer in pivot.columns:

        ax.plot(
            pivot.index,
            pivot[customer],
            marker="o",
            label=customer
        )

    ax.set_title(
        "Customer Order Quantity Trend"
    )

    ax.set_xlabel("Time")
    ax.set_ylabel("Order Quantity")

    ax.legend()

    ax.grid(
        True,
        alpha=0.3
    )

    fig.autofmt_xdate()

    return figure_to_base64(fig)


# ======================================================
# 증가/감소 그래프
# ======================================================

def create_growth_chart(df):

    fig, ax = plt.subplots(
        figsize=(9, 5)
    )

    ax.bar(
        df["customer_id"],
        df["growth"]
    )

    ax.axhline(
        0,
        linewidth=1
    )

    ax.set_title(
        "Customer Order Growth"
    )

    ax.set_xlabel("Customer")
    ax.set_ylabel("Growth Quantity")

    return figure_to_base64(fig)


# ======================================================
# 매출 그래프
# ======================================================

def create_sales_chart(df):

    fig, ax = plt.subplots(
        figsize=(9, 5)
    )

    ax.bar(
        df["customer_id"],
        df["total_sales"]
    )

    ax.set_title(
        "Customer Sales"
    )

    ax.set_xlabel("Customer")
    ax.set_ylabel("Sales")

    return figure_to_base64(fig)


# ======================================================
# 증가량 결과 설명
# ======================================================

def explain_growth(df):

    if df.empty:
        return "분석할 데이터가 없습니다."

    top = df.iloc[0]
    bottom = df.iloc[-1]

    message = (
        f"가장 많이 증가한 고객은 "
        f"{top['customer_id']}이며, "
        f"증가량은 {int(top['growth'])}입니다."
    )

    if bottom["growth"] < 0:

        message += (
            f" 가장 많이 감소한 고객은 "
            f"{bottom['customer_id']}이며, "
            f"감소량은 {abs(int(bottom['growth']))}입니다."
        )

    return message


# ======================================================
# HTML
# ======================================================

def make_page(
    question="",
    result="",
    chart=None,
    table_html=""
):

    question = html.escape(question)
    result = html.escape(result)

    chart_html = ""

    if chart:

        chart_html = f"""
        <div class="chart">
            <img
                src="data:image/png;base64,{chart}"
                alt="analysis chart"
            />
        </div>
        """

    return f"""
    <!DOCTYPE html>

    <html lang="ko">

    <head>

        <meta charset="UTF-8">

        <title>
            Customer Analysis Agent
        </title>

        <style>

            body {{
                font-family:
                    Arial,
                    sans-serif;

                max-width: 1100px;

                margin: 40px auto;

                padding: 0 20px;

                background: #f5f5f5;
            }}

            .box {{
                background: white;
                padding: 25px;
                border-radius: 10px;
                margin-bottom: 20px;
            }}

            h1 {{
                margin-top: 0;
            }}

            input {{
                width: 75%;
                padding: 12px;
                font-size: 16px;
            }}

            button {{
                padding: 12px 20px;
                font-size: 16px;
                cursor: pointer;
            }}

            .result {{
                font-size: 18px;
                line-height: 1.7;
            }}

            .chart img {{
                width: 100%;
                max-width: 950px;
            }}

            table {{
                border-collapse: collapse;
                width: 100%;
            }}

            th,
            td {{
                border: 1px solid #ddd;
                padding: 8px;
                text-align: center;
            }}

            th {{
                background: #eee;
            }}

        </style>

    </head>

    <body>

        <div class="box">

            <h1>
                Customer Analysis Agent
            </h1>

            <form
                action="/analyze"
                method="post"
            >

                <input
                    type="text"
                    name="question"
                    value="{question}"
                    placeholder="예: 고객별 주문량 추이를 보여줘"
                    required
                />

                <button type="submit">
                    분석
                </button>

            </form>

            <p>
                예:
                고객별 주문량 추이 /
                주문량 증가 고객 /
                고객별 매출
            </p>

        </div>


        {
            f'''
            <div class="box result">

                <h2>
                    분석 결과
                </h2>

                <p>
                    {result}
                </p>

                {chart_html}

                {table_html}

            </div>
            '''
            if result
            else ""
        }

    </body>

    </html>
    """


# ======================================================
# 초기 화면
# ======================================================

@app.get(
    "/",
    response_class=HTMLResponse
)
def home():

    return make_page()


# ======================================================
# 분석 API
# ======================================================

@app.post(
    "/analyze",
    response_class=HTMLResponse
)
def analyze(
    question: str = Form(...)
):

    conn = None

    try:

        action = decide_action(
            question
        )

        conn = connect_db()


        # ----------------------------------------------
        # 증가/감소
        # ----------------------------------------------

        if action == "growth":

            df = get_customer_growth(
                conn,
                minutes=5
            )

            if df.empty:

                return make_page(
                    question,
                    "분석 데이터가 없습니다."
                )

            chart = create_growth_chart(
                df
            )

            result = explain_growth(
                df
            )


        # ----------------------------------------------
        # 매출
        # ----------------------------------------------

        elif action == "sales":

            df = get_customer_sales(
                conn,
                minutes=10
            )

            if df.empty:

                return make_page(
                    question,
                    "분석 데이터가 없습니다."
                )

            chart = create_sales_chart(
                df
            )

            top = df.iloc[0]

            result = (
                f"최근 분석 구간에서 "
                f"매출이 가장 높은 고객은 "
                f"{top['customer_id']}이며, "
                f"매출액은 "
                f"{int(top['total_sales']):,}입니다."
            )


        # ----------------------------------------------
        # 주문 Trend
        # ----------------------------------------------

        else:

            df = get_customer_trend(
                conn,
                minutes=10
            )

            if df.empty:

                return make_page(
                    question,
                    "분석 데이터가 없습니다."
                )

            chart = create_trend_chart(
                df
            )

            result = (
                "최근 데이터 기준 "
                "고객별 주문량 추이입니다."
            )


        table_html = df.to_html(
            index=False,
            border=0
        )


        return make_page(
            question,
            result,
            chart,
            table_html
        )


    except Exception as e:

        return make_page(
            question,
            f"분석 중 오류 발생: {str(e)}"
        )


    finally:

        if conn:
            conn.close()


# ======================================================
# Kubernetes Health Check
# ======================================================

@app.get("/health")
def health():

    try:

        conn = connect_db()

        with conn.cursor() as cursor:

            cursor.execute(
                "SELECT 1"
            )

            cursor.fetchone()

        conn.close()

        return {
            "status": "ok",
            "database": "connected"
        }

    except Exception as e:

        return {
            "status": "error",
            "message": str(e)
        }