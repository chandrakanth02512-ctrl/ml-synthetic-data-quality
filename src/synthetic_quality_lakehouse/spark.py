"""Real PySpark normalization/windowing and Spark SQL quality classification."""

from dataclasses import dataclass

from pyspark.sql import Window
from pyspark.sql import functions as F
from pyspark.sql import types as T

from synthetic_quality.schema import (
    COUNTRY_CURRENCY,
    MERCHANT_CATEGORIES,
    REQUIRED_COLUMNS,
    STRING_COLUMNS,
    UNITS_PER_USD,
)

CHECKS = (
    "MALFORMED_JSON",
    "SCHEMA_MISMATCH",
    "INVALID_TYPES",
    "NULL_REQUIRED",
    "BLANK_REQUIRED",
    "INVALID_AMOUNT",
    "INVALID_TIMESTAMP",
    "UNSUPPORTED_CURRENCY",
    "UNSUPPORTED_CATEGORY",
    "UNSUPPORTED_COUNTRY",
    "COUNTRY_CURRENCY_MISMATCH",
    "DUPLICATE_TRANSACTION_ID",
)
META = [
    "run_id",
    "source_fingerprint",
    "source_row_id",
    "source_row_number",
    "source_record_sha256",
]


@dataclass
class SparkOutputs:
    clean: object
    quarantine: object
    quality: object
    ledger: object
    classified: object


def transform(spark, records, run_id, fingerprint):
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    fields = [T.StructField(c, T.StringType(), True) for c in REQUIRED_COLUMNS]
    fields += [T.StructField("source_row_number", T.LongType(), False)]
    fields += [
        T.StructField(c, T.StringType(), False)
        for c in ("source_row_id", "source_record_sha256", "raw_payload")
    ]
    fields += [T.StructField("parser_errors", T.ArrayType(T.StringType()), False)]
    raw = spark.createDataFrame(records, T.StructType(fields))
    rates = F.create_map(*[F.lit(v) for pair in sorted(UNITS_PER_USD.items()) for v in pair])
    countries = F.create_map(*[F.lit(v) for pair in sorted(COUNTRY_CURRENCY.items()) for v in pair])
    normalized = (
        raw.withColumn("run_id", F.lit(run_id))
        .withColumn("source_fingerprint", F.lit(fingerprint))
        .withColumn("_amount", F.expr("try_cast(transaction_amount AS DOUBLE)"))
        .withColumn("_time", F.expr("try_to_timestamp(timestamp)"))
        .withColumn("_rate", rates[F.col("currency")])
        .withColumn("_expected_currency", countries[F.col("country")])
        .withColumn(
            "_duplicates", F.count("transaction_id").over(Window.partitionBy("transaction_id"))
        )
    )
    normalized.createOrReplaceTempView("idq_source")

    def quoted(xs):
        return ",".join("'" + x + "'" for x in xs)  # fixed shared schema constants

    predicates = {
        "NULL_REQUIRED": " OR ".join(f"`{c}` IS NULL" for c in REQUIRED_COLUMNS),
        "BLANK_REQUIRED": " OR ".join(f"`{c}` RLIKE r'(?U)^\\s*$'" for c in STRING_COLUMNS),
        "INVALID_AMOUNT": (
            "_amount IS NULL OR isnan(_amount) OR abs(_amount) = double('Infinity') "
            "OR _amount <= 0 OR abs(_amount / _rate) = double('Infinity')"
        ),
        "INVALID_TIMESTAMP": "_time IS NULL OR NOT timestamp RLIKE '(Z|[+]00:00)$'",
        "UNSUPPORTED_CURRENCY": "currency NOT IN (" + quoted(UNITS_PER_USD) + ")",
        "UNSUPPORTED_CATEGORY": "merchant_category NOT IN (" + quoted(MERCHANT_CATEGORIES) + ")",
        "UNSUPPORTED_COUNTRY": "country NOT IN (" + quoted(COUNTRY_CURRENCY) + ")",
        "COUNTRY_CURRENCY_MISMATCH": (
            "_expected_currency IS NOT NULL AND currency <> _expected_currency"
        ),
        "DUPLICATE_TRANSACTION_ID": (
            "transaction_id IS NOT NULL AND NOT (transaction_id RLIKE r'(?U)^\\s*$') "
            "AND _duplicates > 1"
        ),
    }
    errors = ",".join(
        f"IF(coalesce(({condition}), false), '{name}', NULL)"
        for name, condition in predicates.items()
    )
    classified = spark.sql(f"""
        SELECT *, array_distinct(concat(parser_errors,
            filter(array({errors}), x -> x IS NOT NULL))) AS error_codes,
            _amount / _rate AS transaction_amount_usd
        FROM idq_source
    """).cache()
    classified.createOrReplaceTempView("idq_classified")
    good = classified.filter(F.size("error_codes") == 0)
    clean = good.select(
        *META,
        "transaction_id",
        "customer_id",
        F.col("_time").alias("timestamp"),
        F.col("_amount").alias("transaction_amount"),
        "merchant_category",
        "currency",
        "country",
        "transaction_amount_usd",
        F.to_date("_time").alias("transaction_date"),
    )
    quarantine = classified.filter(F.size("error_codes") > 0).select(
        *META, "raw_payload", "error_codes"
    )
    quality = spark.sql(f"""
        SELECT run_id, source_fingerprint, check_name, count(*) AS evaluated_rows,
               sum(IF(array_contains(error_codes, check_name), 1L, 0L)) AS failed_rows,
               sum(IF(array_contains(error_codes, check_name), 1L, 0L)) = 0 AS passed
        FROM idq_classified
        LATERAL VIEW explode(array({quoted(CHECKS)})) c AS check_name
        GROUP BY run_id, source_fingerprint, check_name
    """)
    ledger = classified.select(
        *META,
        F.when(F.size("error_codes") == 0, "clean").otherwise("quarantine").alias("disposition"),
        F.when(F.size("error_codes") == 0, F.col("transaction_amount_usd")).alias("amount_usd"),
    )
    return SparkOutputs(clean, quarantine, quality, ledger, classified)
