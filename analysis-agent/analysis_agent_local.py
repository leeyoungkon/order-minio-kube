import os
import sys
import pymysql
import pandas as pd
import matplotlib.pyplot as plt
from datetime import datetime


# ======================================================
# DB 설정
# ======================================================

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "3307"))
DB_NAME = os.getenv("DB_NAME", "analytics")
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "root1234")


# 그래프 저장 폴더
OUTPUT_DIR = os.getenv("OUTPUT_DIR", "./output")

os.makedirs(OUTPUT_DIR, exist_ok=True)


# ======================================================
# DB 연결
# ======================================================

def connect_db():

    print("[Agent] Analytics DB 연결 시도...")

    conn = pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset="utf8mb4"
    )

    print("[Agent] Analytics DB 연결 성공")

    return conn


# ======================================================
# Tool 1
# 고객별 주문량 추이 조회
# ======================================================

def get_customer_trend(conn, minutes=10):

    sql = """
    SELECT
        minute_time,
        customer_id,
        total_quantity
    FROM customer_order_minute
    WHERE minute_time >= NOW() - INTERVAL %s MINUTE
    ORDER BY minute_time, customer_id
    """

    df = pd.read_sql(
        sql,
        conn,
        params=(minutes,)
    )

    return df


# ======================================================
# Tool 2
# 고객별 최근 주문 증가량 계산
# ======================================================

def get_customer_growth(conn, minutes=5):

    sql = """
    SELECT
        minute_time,
        customer_id,
        total_quantity
    FROM customer_order_minute
    WHERE minute_time >= NOW() - INTERVAL %s MINUTE
    ORDER BY customer_id, minute_time
    """

    # 최근 구간 + 이전 구간이 필요하므로
    # 실제 조회 범위는 2배
    df = pd.read_sql(
        sql,
        conn,
        params=(minutes * 2,)
    )

    if df.empty:
        return pd.DataFrame()

    now = pd.Timestamp.now()

    recent_start = now - pd.Timedelta(minutes=minutes)
    previous_start = now - pd.Timedelta(minutes=minutes * 2)

    df["minute_time"] = pd.to_datetime(
        df["minute_time"]
    )

    recent_df = df[
        df["minute_time"] >= recent_start
    ]

    previous_df = df[
        (df["minute_time"] >= previous_start)
        &
        (df["minute_time"] < recent_start)
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


    result["growth_rate"] = result.apply(
        lambda row:
        (
            (row["growth"] / row["previous_quantity"]) * 100
            if row["previous_quantity"] > 0
            else 0
        ),
        axis=1
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

    sql = """
    SELECT
        customer_id,
        SUM(total_amount) AS total_sales,
        SUM(total_quantity) AS total_quantity,
        SUM(order_count) AS order_count
    FROM customer_order_minute
    WHERE minute_time >= NOW() - INTERVAL %s MINUTE
    GROUP BY customer_id
    ORDER BY total_sales DESC
    """

    df = pd.read_sql(
        sql,
        conn,
        params=(minutes,)
    )

    return df


# ======================================================
# Tool 4
# Trend 그래프
# ======================================================

def plot_customer_trend(df):

    if df.empty:
        print("[Agent] 그래프로 표시할 데이터가 없습니다.")
        return None


    pivot_df = df.pivot_table(
        index="minute_time",
        columns="customer_id",
        values="total_quantity",
        aggfunc="sum",
        fill_value=0
    )


    plt.figure(
        figsize=(10, 6)
    )


    for customer in pivot_df.columns:

        plt.plot(
            pivot_df.index,
            pivot_df[customer],
            marker="o",
            label=customer
        )


    plt.title(
        "Customer Order Quantity Trend"
    )

    plt.xlabel(
        "Time"
    )

    plt.ylabel(
        "Order Quantity"
    )

    plt.legend()

    plt.xticks(
        rotation=45
    )

    plt.grid(
        True,
        alpha=0.3
    )

    plt.tight_layout()


    filename = os.path.join(
        OUTPUT_DIR,
        "customer_trend.png"
    )


    plt.savefig(
        filename,
        dpi=150
    )

    plt.close()


    print(
        "[Agent] Trend 그래프 생성:",
        filename
    )

    return filename


# ======================================================
# Tool 5
# Growth 그래프
# ======================================================

def plot_customer_growth(df):

    if df.empty:
        print("[Agent] 증가량 데이터가 없습니다.")
        return None


    plt.figure(
        figsize=(8, 5)
    )


    plt.bar(
        df["customer_id"],
        df["growth"]
    )


    plt.axhline(
        0,
        linewidth=1
    )


    plt.title(
        "Customer Order Growth"
    )

    plt.xlabel(
        "Customer"
    )

    plt.ylabel(
        "Growth Quantity"
    )


    plt.tight_layout()


    filename = os.path.join(
        OUTPUT_DIR,
        "customer_growth.png"
    )


    plt.savefig(
        filename,
        dpi=150
    )

    plt.close()


    print(
        "[Agent] Growth 그래프 생성:",
        filename
    )

    return filename


# ======================================================
# Tool 6
# 매출 그래프
# ======================================================

def plot_customer_sales(df):

    if df.empty:
        print("[Agent] 매출 데이터가 없습니다.")
        return None


    plt.figure(
        figsize=(8, 5)
    )


    plt.bar(
        df["customer_id"],
        df["total_sales"]
    )


    plt.title(
        "Customer Sales"
    )

    plt.xlabel(
        "Customer"
    )

    plt.ylabel(
        "Sales Amount"
    )


    plt.tight_layout()


    filename = os.path.join(
        OUTPUT_DIR,
        "customer_sales.png"
    )


    plt.savefig(
        filename,
        dpi=150
    )

    plt.close()


    print(
        "[Agent] Sales 그래프 생성:",
        filename
    )

    return filename


# ======================================================
# 분석 결과 설명
# ======================================================

def explain_growth(df):

    if df.empty:
        return "분석할 데이터가 없습니다."


    top = df.iloc[0]

    bottom = df.iloc[-1]


    message = "\n[Analysis Result]\n"

    message += (
        f"- 가장 많이 증가한 고객: "
        f"{top['customer_id']}\n"
    )

    message += (
        f"  증가량: "
        f"{int(top['growth'])}\n"
    )


    message += (
        f"- 가장 많이 감소한 고객: "
        f"{bottom['customer_id']}\n"
    )

    message += (
        f"  증가량: "
        f"{int(bottom['growth'])}\n"
    )


    return message


# ======================================================
# Agent Decision Logic
# ======================================================

def decide_action(question):

    question = question.lower()


    # 주문 증가/감소 분석
    if (
        "증가" in question
        or "감소" in question
        or "성장" in question
    ):

        return "growth"


    # 매출 분석
    if (
        "매출" in question
        or "금액" in question
    ):

        return "sales"


    # 추이 / 트렌드
    if (
        "추이" in question
        or "트렌드" in question
        or "그래프" in question
    ):

        return "trend"


    return "trend"


# ======================================================
# Analysis Agent
# ======================================================

def analysis_agent(question):

    print()
    print("==============================")
    print(" Analysis Agent")
    print("==============================")

    print(
        "[User]",
        question
    )


    action = decide_action(
        question
    )


    print(
        "[Agent Decision]",
        action
    )


    conn = None


    try:

        conn = connect_db()


        # ----------------------------------------------
        # 고객 증가량 분석
        # ----------------------------------------------

        if action == "growth":

            print(
                "[Agent] 고객별 증가량 분석 실행"
            )

            df = get_customer_growth(
                conn,
                minutes=5
            )


            print()
            print(df)


            chart = plot_customer_growth(
                df
            )


            explanation = explain_growth(
                df
            )


            print(
                explanation
            )


            return {
                "action": action,
                "data": df,
                "chart": chart,
                "explanation": explanation
            }


        # ----------------------------------------------
        # 매출 분석
        # ----------------------------------------------

        elif action == "sales":

            print(
                "[Agent] 고객별 매출 분석 실행"
            )


            df = get_customer_sales(
                conn,
                minutes=10
            )


            print()
            print(df)


            chart = plot_customer_sales(
                df
            )


            return {
                "action": action,
                "data": df,
                "chart": chart
            }


        # ----------------------------------------------
        # Trend 분석
        # ----------------------------------------------

        else:

            print(
                "[Agent] 고객별 주문량 Trend 분석 실행"
            )


            df = get_customer_trend(
                conn,
                minutes=10
            )


            print()
            print(df)


            chart = plot_customer_trend(
                df
            )


            return {
                "action": action,
                "data": df,
                "chart": chart
            }


    except Exception as e:

        print(
            "[Agent Error]",
            repr(e)
        )


    finally:

        if conn:
            conn.close()


# ======================================================
# CLI
# ======================================================

def main():

    print("==============================")
    print(" Customer Analysis Agent")
    print("==============================")


    while True:

        question = input(
            "\n분석 질문 입력(q=종료): "
        )


        if question.lower() in [
            "q",
            "quit",
            "exit"
        ]:

            print(
                "Analysis Agent 종료"
            )

            break


        analysis_agent(
            question
        )


if __name__ == "__main__":
    main()