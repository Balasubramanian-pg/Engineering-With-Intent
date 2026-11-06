# labs/03-dlt-scaffold.py
# Generates a standardized Lakeflow Declarative Pipelines scaffold targeting DBR 15.4 LTS.
from pyspark import pipelines as dp
from pyspark.sql import functions as F

@dp.table(name="bronze_orders_raw", comment="Raw streaming ingestion from cloud storage")
def bronze_raw():
    return spark.readStream.format("cloudFiles").option("cloudFiles.format", "json").load("/Volumes/retail_prod/raw/orders/")

@dp.table(name="silver_orders_clean", comment="Validated silver records")
@dp.expect_or_drop("valid_id", "order_id IS NOT NULL")
@dp.expect_or_drop("positive_amount", "net_amount > 0")
def silver_clean():
    return (
        dp.read_stream("bronze_orders_raw")
        .select(
            F.col("order_id").cast("bigint"),
            F.col("customer_id").cast("bigint"),
            F.col("net_amount").cast("decimal(10,2)"),
            F.to_date("event_timestamp").alias("event_date")
        )
    )

@dp.table(name="gold_orders_daily", comment="Pre-aggregated daily metrics")
@dp.expect_all({"valid_date": "event_date IS NOT NULL"})
def gold_daily():
    return (
        dp.read("silver_orders_clean")
        .groupBy("event_date", "customer_id")
        .agg(F.count("order_id").alias("order_count"), F.sum("net_amount").alias("total_revenue"))
    )

if __name__ == '__main__':
    print("Lakeflow Declarative Pipeline definition verified.")
