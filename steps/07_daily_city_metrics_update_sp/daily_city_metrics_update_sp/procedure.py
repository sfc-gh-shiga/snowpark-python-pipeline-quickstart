#------------------------------------------------------------------------------
# Hands-On Lab: Data Engineering with Snowpark
# Script:       07_daily_city_metrics_process_sp/app.py
# Author:       Jeremiah Hansen, Caleb Baechtold
# Last Updated: 1/9/2023
#------------------------------------------------------------------------------

import time
from snowflake.snowpark import Session
import snowflake.snowpark.types as T
import snowflake.snowpark.functions as F


def table_exists(session, schema='', name=''):
    exists = session.sql("SELECT EXISTS (SELECT * FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = '{}' AND TABLE_NAME = '{}') AS TABLE_EXISTS".format(schema, name)).collect()[0]['TABLE_EXISTS']
    return exists

def create_daily_city_metrics_table(session):
    SHARED_COLUMNS= [T.StructField("DATE", T.DateType()),
                                        T.StructField("CITY_NAME", T.StringType()),
                                        T.StructField("COUNTRY_DESC", T.StringType()),
                                        T.StructField("DAILY_SALES", T.StringType()),
                                        T.StructField("AVG_TEMPERATURE_FAHRENHEIT", T.DecimalType()),
                                        T.StructField("AVG_TEMPERATURE_CELSIUS", T.DecimalType()),
                                        T.StructField("AVG_PRECIPITATION_INCHES", T.DecimalType()),
                                        T.StructField("AVG_PRECIPITATION_MILLIMETERS", T.DecimalType()),
                                        T.StructField("MAX_WIND_SPEED_100M_MPH", T.DecimalType()),
                                    ]
    DAILY_CITY_METRICS_COLUMNS = [*SHARED_COLUMNS, T.StructField("META_UPDATED_AT", T.TimestampType())]
    DAILY_CITY_METRICS_SCHEMA = T.StructType(DAILY_CITY_METRICS_COLUMNS)

    dcm = session.create_dataframe([[None]*len(DAILY_CITY_METRICS_SCHEMA.names)], schema=DAILY_CITY_METRICS_SCHEMA) \
                        .na.drop() \
                        .write.mode('overwrite').save_as_table('ANALYTICS.DAILY_CITY_METRICS')
    dcm = session.table('ANALYTICS.DAILY_CITY_METRICS')


def merge_daily_city_metrics(session):
    _ = session.sql('ALTER WAREHOUSE HOL_WH SET WAREHOUSE_SIZE = XLARGE WAIT_FOR_COMPLETION = TRUE').collect()

    print("{} records in stream".format(session.table('HARMONIZED.ORDERS_STREAM').count()))
    orders_stream_dates = session.table('HARMONIZED.ORDERS_STREAM').select(F.col("ORDER_TS_DATE").alias("DATE")).distinct()
    orders_stream_dates.limit(5).show()

    orders = session.table("HARMONIZED.ORDERS_STREAM").group_by(F.col('ORDER_TS_DATE'), F.col('PRIMARY_CITY'), F.col('COUNTRY')) \
                                        .agg(F.sum(F.col("PRICE")).as_("price_nulls")) \
                                        .with_column("DAILY_SALES", F.call_builtin("ZEROIFNULL", F.col("price_nulls"))) \
                                        .select(F.col('ORDER_TS_DATE').alias("DATE"), F.col("PRIMARY_CITY").alias("CITY_NAME"), \
                                        F.col("COUNTRY").alias("COUNTRY_DESC"), F.col("DAILY_SALES"))
#    orders.limit(5).show()

    weather_pc = session.table("FROSTBYTE_WEATHERSOURCE.ONPOINT_ID.POSTAL_CODES")
    countries = session.table("RAW_POS.COUNTRY")
    weather = session.table("FROSTBYTE_WEATHERSOURCE.ONPOINT_ID.HISTORY_DAY")
    weather = weather.join(weather_pc, (weather['POSTAL_CODE'] == weather_pc['POSTAL_CODE']) & (weather['COUNTRY'] == weather_pc['COUNTRY']), rsuffix='_pc')
    weather = weather.join(countries, (weather['COUNTRY'] == countries['ISO_COUNTRY']) & (weather['CITY_NAME'] == countries['CITY']), rsuffix='_c')
    weather = weather.join(orders_stream_dates, weather['DATE_VALID_STD'] == orders_stream_dates['DATE'])

    weather_agg = weather.group_by(F.col('DATE_VALID_STD'), F.col('CITY_NAME'), F.col('COUNTRY_C')) \
                        .agg( \
                            F.avg('AVG_TEMPERATURE_AIR_2M_F').alias("AVG_TEMPERATURE_F"), \
                            F.avg(F.call_udf("ANALYTICS.FAHRENHEIT_TO_CELSIUS_UDF", F.col("AVG_TEMPERATURE_AIR_2M_F"))).alias("AVG_TEMPERATURE_C"), \
                            F.avg("TOT_PRECIPITATION_IN").alias("AVG_PRECIPITATION_IN"), \
                            F.avg(F.call_udf("ANALYTICS.INCH_TO_MILLIMETER_UDF", F.col("TOT_PRECIPITATION_IN"))).alias("AVG_PRECIPITATION_MM"), \
                            F.max(F.col("MAX_WIND_SPEED_100M_MPH")).alias("MAX_WIND_SPEED_100M_MPH") \
                        ) \
                        .select(F.col("DATE_VALID_STD").alias("DATE"), F.col("CITY_NAME"), F.col("COUNTRY_C").alias("COUNTRY_DESC"), \
                            F.round(F.col("AVG_TEMPERATURE_F"), 2).alias("AVG_TEMPERATURE_FAHRENHEIT"), \
                            F.round(F.col("AVG_TEMPERATURE_C"), 2).alias("AVG_TEMPERATURE_CELSIUS"), \
                            F.round(F.col("AVG_PRECIPITATION_IN"), 2).alias("AVG_PRECIPITATION_INCHES"), \
                            F.round(F.col("AVG_PRECIPITATION_MM"), 2).alias("AVG_PRECIPITATION_MILLIMETERS"), \
                            F.col("MAX_WIND_SPEED_100M_MPH")
                            )
#    weather_agg.limit(5).show()

    daily_city_metrics_stg = orders.join(weather_agg, (orders['DATE'] == weather_agg['DATE']) & (orders['CITY_NAME'] == weather_agg['CITY_NAME']) & (orders['COUNTRY_DESC'] == weather_agg['COUNTRY_DESC']), \
                        how='left', rsuffix='_w') \
                    .select("DATE", "CITY_NAME", "COUNTRY_DESC", "DAILY_SALES", \
                        "AVG_TEMPERATURE_FAHRENHEIT", "AVG_TEMPERATURE_CELSIUS", \
                        "AVG_PRECIPITATION_INCHES", "AVG_PRECIPITATION_MILLIMETERS", \
                        "MAX_WIND_SPEED_100M_MPH")
#    daily_city_metrics_stg.limit(5).show()

    cols_to_update = {c: daily_city_metrics_stg[c] for c in daily_city_metrics_stg.schema.names}
    metadata_col_to_update = {"META_UPDATED_AT": F.current_timestamp()}
    updates = {**cols_to_update, **metadata_col_to_update}

    dcm = session.table('ANALYTICS.DAILY_CITY_METRICS')
    dcm.merge(daily_city_metrics_stg, (dcm['DATE'] == daily_city_metrics_stg['DATE']) & (dcm['CITY_NAME'] == daily_city_metrics_stg['CITY_NAME']) & (dcm['COUNTRY_DESC'] == daily_city_metrics_stg['COUNTRY_DESC']), \
                        [F.when_matched().update(updates), F.when_not_matched().insert(updates)])

    _ = session.sql('ALTER WAREHOUSE HOL_WH SET WAREHOUSE_SIZE = XSMALL').collect()

def main(session: Session) -> str:
    # Create the DAILY_CITY_METRICS table if it doesn't exist
    if not table_exists(session, schema='ANALYTICS', name='DAILY_CITY_METRICS'):
        create_daily_city_metrics_table(session)
    
    merge_daily_city_metrics(session)
#    session.table('ANALYTICS.DAILY_CITY_METRICS').limit(5).show()

    return f"Successfully processed DAILY_CITY_METRICS"


# For local debugging
# Be aware you may need to type-convert arguments if you add input parameters
if __name__ == '__main__':
    # Create a local Snowpark session
    with Session.builder.getOrCreate() as session:
        import sys
        if len(sys.argv) > 1:
            print(main(session, *sys.argv[1:]))  # type: ignore
        else:
            print(main(session))  # type: ignore


### MERGE  INTO ANALYTICS.DAILY_CITY_METRICS USING ( SELECT "DATE" AS "r_0003_DATE", "CITY_NAME" AS "r_0003_CITY_NAME", "COUNTRY_DESC" AS "r_0003_COUNTRY_DESC", "DAILY_SALES" AS "r_0003_DAILY_SALES", "AVG_TEMPERATURE_FAHRENHEIT" AS "r_0003_AVG_TEMPERATURE_FAHRENHEIT", "AVG_TEMPERATURE_CELSIUS" AS "r_0003_AVG_TEMPERATURE_CELSIUS", "AVG_PRECIPITATION_INCHES" AS "r_0003_AVG_PRECIPITATION_INCHES", "AVG_PRECIPITATION_MILLIMETERS" AS "r_0003_AVG_PRECIPITATION_MILLIMETERS", "MAX_WIND_SPEED_100M_MPH" AS "r_0003_MAX_WIND_SPEED_100M_MPH" FROM ( SELECT  *  FROM (( SELECT "DATE" AS "DATE", "CITY_NAME" AS "CITY_NAME", "COUNTRY_DESC" AS "COUNTRY_DESC", "DAILY_SALES" AS "DAILY_SALES" FROM ( SELECT "ORDER_TS_DATE" AS "DATE", "PRIMARY_CITY" AS "CITY_NAME", "COUNTRY" AS "COUNTRY_DESC", ZEROIFNULL("PRICE_NULLS") AS "DAILY_SALES" FROM ( SELECT "ORDER_TS_DATE", "PRIMARY_CITY", "COUNTRY", sum("PRICE") AS "PRICE_NULLS" FROM ( SELECT  *  FROM HARMONIZED.ORDERS_STREAM) GROUP BY "ORDER_TS_DATE", "PRIMARY_CITY", "COUNTRY"))) AS SNOWPARK_LEFT LEFT OUTER JOIN ( SELECT "DATE" AS "DATE_W", "CITY_NAME" AS "CITY_NAME_W", "COUNTRY_DESC" AS "COUNTRY_DESC_W", "AVG_TEMPERATURE_FAHRENHEIT" AS "AVG_TEMPERATURE_FAHRENHEIT", "AVG_TEMPERATURE_CELSIUS" AS "AVG_TEMPERATURE_CELSIUS", "AVG_PRECIPITATION_INCHES" AS "AVG_PRECIPITATION_INCHES", "AVG_PRECIPITATION_MILLIMETERS" AS "AVG_PRECIPITATION_MILLIMETERS", "MAX_WIND_SPEED_100M_MPH" AS "MAX_WIND_SPEED_100M_MPH" FROM ( SELECT "DATE_VALID_STD" AS "DATE", "CITY_NAME", "COUNTRY_C" AS "COUNTRY_DESC", round("AVG_TEMPERATURE_F", 2) AS "AVG_TEMPERATURE_FAHRENHEIT", round("AVG_TEMPERATURE_C", 2) AS "AVG_TEMPERATURE_CELSIUS", round("AVG_PRECIPITATION_IN", 2) AS "AVG_PRECIPITATION_INCHES", round("AVG_PRECIPITATION_MM", 2) AS "AVG_PRECIPITATION_MILLIMETERS", "MAX_WIND_SPEED_100M_MPH" FROM ( SELECT "DATE_VALID_STD", "CITY_NAME", "COUNTRY_C", avg("AVG_TEMPERATURE_AIR_2M_F") AS "AVG_TEMPERATURE_F", avg(ANALYTICS.FAHRENHEIT_TO_CELSIUS_UDF("AVG_TEMPERATURE_AIR_2M_F")) AS "AVG_TEMPERATURE_C", avg("TOT_PRECIPITATION_IN") AS "AVG_PRECIPITATION_IN", avg(ANALYTICS.INCH_TO_MILLIMETER_UDF("TOT_PRECIPITATION_IN")) AS "AVG_PRECIPITATION_MM", max("MAX_WIND_SPEED_100M_MPH") AS "MAX_WIND_SPEED_100M_MPH" FROM ( SELECT  *  FROM (( SELECT "POSTAL_CODE" AS "POSTAL_CODE", "CITY_NAME" AS "CITY_NAME", "COUNTRY" AS "COUNTRY", "DATE_VALID_STD" AS "DATE_VALID_STD", "DOY_STD" AS "DOY_STD", "MIN_TEMPERATURE_AIR_2M_F" AS "MIN_TEMPERATURE_AIR_2M_F", "AVG_TEMPERATURE_AIR_2M_F" AS "AVG_TEMPERATURE_AIR_2M_F", "MAX_TEMPERATURE_AIR_2M_F" AS "MAX_TEMPERATURE_AIR_2M_F", "MIN_TEMPERATURE_WETBULB_2M_F" AS "MIN_TEMPERATURE_WETBULB_2M_F", "AVG_TEMPERATURE_WETBULB_2M_F" AS "AVG_TEMPERATURE_WETBULB_2M_F", "MAX_TEMPERATURE_WETBULB_2M_F" AS "MAX_TEMPERATURE_WETBULB_2M_F", "MIN_TEMPERATURE_DEWPOINT_2M_F" AS "MIN_TEMPERATURE_DEWPOINT_2M_F", "AVG_TEMPERATURE_DEWPOINT_2M_F" AS "AVG_TEMPERATURE_DEWPOINT_2M_F", "MAX_TEMPERATURE_DEWPOINT_2M_F" AS "MAX_TEMPERATURE_DEWPOINT_2M_F", "MIN_TEMPERATURE_FEELSLIKE_2M_F" AS "MIN_TEMPERATURE_FEELSLIKE_2M_F", "AVG_TEMPERATURE_FEELSLIKE_2M_F" AS "AVG_TEMPERATURE_FEELSLIKE_2M_F", "MAX_TEMPERATURE_FEELSLIKE_2M_F" AS "MAX_TEMPERATURE_FEELSLIKE_2M_F", "MIN_TEMPERATURE_WINDCHILL_2M_F" AS "MIN_TEMPERATURE_WINDCHILL_2M_F", "AVG_TEMPERATURE_WINDCHILL_2M_F" AS "AVG_TEMPERATURE_WINDCHILL_2M_F", "MAX_TEMPERATURE_WINDCHILL_2M_F" AS "MAX_TEMPERATURE_WINDCHILL_2M_F", "MIN_TEMPERATURE_HEATINDEX_2M_F" AS "MIN_TEMPERATURE_HEATINDEX_2M_F", "AVG_TEMPERATURE_HEATINDEX_2M_F" AS "AVG_TEMPERATURE_HEATINDEX_2M_F", "MAX_TEMPERATURE_HEATINDEX_2M_F" AS "MAX_TEMPERATURE_HEATINDEX_2M_F", "MIN_HUMIDITY_RELATIVE_2M_PCT" AS "MIN_HUMIDITY_RELATIVE_2M_PCT", "AVG_HUMIDITY_RELATIVE_2M_PCT" AS "AVG_HUMIDITY_RELATIVE_2M_PCT", "MAX_HUMIDITY_RELATIVE_2M_PCT" AS "MAX_HUMIDITY_RELATIVE_2M_PCT", "MIN_HUMIDITY_SPECIFIC_2M_GPKG" AS "MIN_HUMIDITY_SPECIFIC_2M_GPKG", "AVG_HUMIDITY_SPECIFIC_2M_GPKG" AS "AVG_HUMIDITY_SPECIFIC_2M_GPKG", "MAX_HUMIDITY_SPECIFIC_2M_GPKG" AS "MAX_HUMIDITY_SPECIFIC_2M_GPKG", "MIN_PRESSURE_2M_MB" AS "MIN_PRESSURE_2M_MB", "AVG_PRESSURE_2M_MB" AS "AVG_PRESSURE_2M_MB", "MAX_PRESSURE_2M_MB" AS "MAX_PRESSURE_2M_MB", "MIN_PRESSURE_TENDENCY_2M_MB" AS "MIN_PRESSURE_TENDENCY_2M_MB", "AVG_PRESSURE_TENDENCY_2M_MB" AS "AVG_PRESSURE_TENDENCY_2M_MB", "MAX_PRESSURE_TENDENCY_2M_MB" AS "MAX_PRESSURE_TENDENCY_2M_MB", "MIN_PRESSURE_MEAN_SEA_LEVEL_MB" AS "MIN_PRESSURE_MEAN_SEA_LEVEL_MB", "AVG_PRESSURE_MEAN_SEA_LEVEL_MB" AS "AVG_PRESSURE_MEAN_SEA_LEVEL_MB", "MAX_PRESSURE_MEAN_SEA_LEVEL_MB" AS "MAX_PRESSURE_MEAN_SEA_LEVEL_MB", "MIN_WIND_SPEED_10M_MPH" AS "MIN_WIND_SPEED_10M_MPH", "AVG_WIND_SPEED_10M_MPH" AS "AVG_WIND_SPEED_10M_MPH", "MAX_WIND_SPEED_10M_MPH" AS "MAX_WIND_SPEED_10M_MPH", "AVG_WIND_DIRECTION_10M_DEG" AS "AVG_WIND_DIRECTION_10M_DEG", "MIN_WIND_SPEED_80M_MPH" AS "MIN_WIND_SPEED_80M_MPH", "AVG_WIND_SPEED_80M_MPH" AS "AVG_WIND_SPEED_80M_MPH", "MAX_WIND_SPEED_80M_MPH" AS "MAX_WIND_SPEED_80M_MPH", "AVG_WIND_DIRECTION_80M_DEG" AS "AVG_WIND_DIRECTION_80M_DEG", "MIN_WIND_SPEED_100M_MPH" AS "MIN_WIND_SPEED_100M_MPH", "AVG_WIND_SPEED_100M_MPH" AS "AVG_WIND_SPEED_100M_MPH", "MAX_WIND_SPEED_100M_MPH" AS "MAX_WIND_SPEED_100M_MPH", "AVG_WIND_DIRECTION_100M_DEG" AS "AVG_WIND_DIRECTION_100M_DEG", "TOT_PRECIPITATION_IN" AS "TOT_PRECIPITATION_IN", "TOT_SNOWFALL_IN" AS "TOT_SNOWFALL_IN", "TOT_SNOWDEPTH_IN" AS "TOT_SNOWDEPTH_IN", "MIN_CLOUD_COVER_TOT_PCT" AS "MIN_CLOUD_COVER_TOT_PCT", "AVG_CLOUD_COVER_TOT_PCT" AS "AVG_CLOUD_COVER_TOT_PCT", "MAX_CLOUD_COVER_TOT_PCT" AS "MAX_CLOUD_COVER_TOT_PCT", "MIN_RADIATION_SOLAR_TOTAL_WPM2" AS "MIN_RADIATION_SOLAR_TOTAL_WPM2", "AVG_RADIATION_SOLAR_TOTAL_WPM2" AS "AVG_RADIATION_SOLAR_TOTAL_WPM2", "MAX_RADIATION_SOLAR_TOTAL_WPM2" AS "MAX_RADIATION_SOLAR_TOTAL_WPM2", "TOT_RADIATION_SOLAR_TOTAL_WPM2" AS "TOT_RADIATION_SOLAR_TOTAL_WPM2", "POSTAL_CODE_PC" AS "POSTAL_CODE_PC", "CITY_NAME_PC" AS "CITY_NAME_PC", "COUNTRY_PC" AS "COUNTRY_PC", "COUNTRY_ID" AS "COUNTRY_ID", "COUNTRY_C" AS "COUNTRY_C", "ISO_CURRENCY" AS "ISO_CURRENCY", "ISO_COUNTRY" AS "ISO_COUNTRY", "CITY_ID" AS "CITY_ID", "CITY" AS "CITY", "CITY_POPULATION" AS "CITY_POPULATION" FROM ( SELECT  *  FROM (( SELECT "POSTAL_CODE" AS "POSTAL_CODE", "CITY_NAME" AS "CITY_NAME", "COUNTRY" AS "COUNTRY", "DATE_VALID_STD" AS "DATE_VALID_STD", "DOY_STD" AS "DOY_STD", "MIN_TEMPERATURE_AIR_2M_F" AS "MIN_TEMPERATURE_AIR_2M_F", "AVG_TEMPERATURE_AIR_2M_F" AS "AVG_TEMPERATURE_AIR_2M_F", "MAX_TEMPERATURE_AIR_2M_F" AS "MAX_TEMPERATURE_AIR_2M_F", "MIN_TEMPERATURE_WETBULB_2M_F" AS "MIN_TEMPERATURE_WETBULB_2M_F", "AVG_TEMPERATURE_WETBULB_2M_F" AS "AVG_TEMPERATURE_WETBULB_2M_F", "MAX_TEMPERATURE_WETBULB_2M_F" AS "MAX_TEMPERATURE_WETBULB_2M_F", "MIN_TEMPERATURE_DEWPOINT_2M_F" AS "MIN_TEMPERATURE_DEWPOINT_2M_F", "AVG_TEMPERATURE_DEWPOINT_2M_F" AS "AVG_TEMPERATURE_DEWPOINT_2M_F", "MAX_TEMPERATURE_DEWPOINT_2M_F" AS "MAX_TEMPERATURE_DEWPOINT_2M_F", "MIN_TEMPERATURE_FEELSLIKE_2M_F" AS "MIN_TEMPERATURE_FEELSLIKE_2M_F", "AVG_TEMPERATURE_FEELSLIKE_2M_F" AS "AVG_TEMPERATURE_FEELSLIKE_2M_F", "MAX_TEMPERATURE_FEELSLIKE_2M_F" AS "MAX_TEMPERATURE_FEELSLIKE_2M_F", "MIN_TEMPERATURE_WINDCHILL_2M_F" AS "MIN_TEMPERATURE_WINDCHILL_2M_F", "AVG_TEMPERATURE_WINDCHILL_2M_F" AS "AVG_TEMPERATURE_WINDCHILL_2M_F", "MAX_TEMPERATURE_WINDCHILL_2M_F" AS "MAX_TEMPERATURE_WINDCHILL_2M_F", "MIN_TEMPERATURE_HEATINDEX_2M_F" AS "MIN_TEMPERATURE_HEATINDEX_2M_F", "AVG_TEMPERATURE_HEATINDEX_2M_F" AS "AVG_TEMPERATURE_HEATINDEX_2M_F", "MAX_TEMPERATURE_HEATINDEX_2M_F" AS "MAX_TEMPERATURE_HEATINDEX_2M_F", "MIN_HUMIDITY_RELATIVE_2M_PCT" AS "MIN_HUMIDITY_RELATIVE_2M_PCT", "AVG_HUMIDITY_RELATIVE_2M_PCT" AS "AVG_HUMIDITY_RELATIVE_2M_PCT", "MAX_HUMIDITY_RELATIVE_2M_PCT" AS "MAX_HUMIDITY_RELATIVE_2M_PCT", "MIN_HUMIDITY_SPECIFIC_2M_GPKG" AS "MIN_HUMIDITY_SPECIFIC_2M_GPKG", "AVG_HUMIDITY_SPECIFIC_2M_GPKG" AS "AVG_HUMIDITY_SPECIFIC_2M_GPKG", "MAX_HUMIDITY_SPECIFIC_2M_GPKG" AS "MAX_HUMIDITY_SPECIFIC_2M_GPKG", "MIN_PRESSURE_2M_MB" AS "MIN_PRESSURE_2M_MB", "AVG_PRESSURE_2M_MB" AS "AVG_PRESSURE_2M_MB", "MAX_PRESSURE_2M_MB" AS "MAX_PRESSURE_2M_MB", "MIN_PRESSURE_TENDENCY_2M_MB" AS "MIN_PRESSURE_TENDENCY_2M_MB", "AVG_PRESSURE_TENDENCY_2M_MB" AS "AVG_PRESSURE_TENDENCY_2M_MB", "MAX_PRESSURE_TENDENCY_2M_MB" AS "MAX_PRESSURE_TENDENCY_2M_MB", "MIN_PRESSURE_MEAN_SEA_LEVEL_MB" AS "MIN_PRESSURE_MEAN_SEA_LEVEL_MB", "AVG_PRESSURE_MEAN_SEA_LEVEL_MB" AS "AVG_PRESSURE_MEAN_SEA_LEVEL_MB", "MAX_PRESSURE_MEAN_SEA_LEVEL_MB" AS "MAX_PRESSURE_MEAN_SEA_LEVEL_MB", "MIN_WIND_SPEED_10M_MPH" AS "MIN_WIND_SPEED_10M_MPH", "AVG_WIND_SPEED_10M_MPH" AS "AVG_WIND_SPEED_10M_MPH", "MAX_WIND_SPEED_10M_MPH" AS "MAX_WIND_SPEED_10M_MPH", "AVG_WIND_DIRECTION_10M_DEG" AS "AVG_WIND_DIRECTION_10M_DEG", "MIN_WIND_SPEED_80M_MPH" AS "MIN_WIND_SPEED_80M_MPH", "AVG_WIND_SPEED_80M_MPH" AS "AVG_WIND_SPEED_80M_MPH", "MAX_WIND_SPEED_80M_MPH" AS "MAX_WIND_SPEED_80M_MPH", "AVG_WIND_DIRECTION_80M_DEG" AS "AVG_WIND_DIRECTION_80M_DEG", "MIN_WIND_SPEED_100M_MPH" AS "MIN_WIND_SPEED_100M_MPH", "AVG_WIND_SPEED_100M_MPH" AS "AVG_WIND_SPEED_100M_MPH", "MAX_WIND_SPEED_100M_MPH" AS "MAX_WIND_SPEED_100M_MPH", "AVG_WIND_DIRECTION_100M_DEG" AS "AVG_WIND_DIRECTION_100M_DEG", "TOT_PRECIPITATION_IN" AS "TOT_PRECIPITATION_IN", "TOT_SNOWFALL_IN" AS "TOT_SNOWFALL_IN", "TOT_SNOWDEPTH_IN" AS "TOT_SNOWDEPTH_IN", "MIN_CLOUD_COVER_TOT_PCT" AS "MIN_CLOUD_COVER_TOT_PCT", "AVG_CLOUD_COVER_TOT_PCT" AS "AVG_CLOUD_COVER_TOT_PCT", "MAX_CLOUD_COVER_TOT_PCT" AS "MAX_CLOUD_COVER_TOT_PCT", "MIN_RADIATION_SOLAR_TOTAL_WPM2" AS "MIN_RADIATION_SOLAR_TOTAL_WPM2", "AVG_RADIATION_SOLAR_TOTAL_WPM2" AS "AVG_RADIATION_SOLAR_TOTAL_WPM2", "MAX_RADIATION_SOLAR_TOTAL_WPM2" AS "MAX_RADIATION_SOLAR_TOTAL_WPM2", "TOT_RADIATION_SOLAR_TOTAL_WPM2" AS "TOT_RADIATION_SOLAR_TOTAL_WPM2", "POSTAL_CODE_PC" AS "POSTAL_CODE_PC", "CITY_NAME_PC" AS "CITY_NAME_PC", "COUNTRY_PC" AS "COUNTRY_PC" FROM ( SELECT  *  FROM (( SELECT "POSTAL_CODE" AS "POSTAL_CODE", "CITY_NAME" AS "CITY_NAME", "COUNTRY" AS "COUNTRY", "DATE_VALID_STD" AS "DATE_VALID_STD", "DOY_STD" AS "DOY_STD", "MIN_TEMPERATURE_AIR_2M_F" AS "MIN_TEMPERATURE_AIR_2M_F", "AVG_TEMPERATURE_AIR_2M_F" AS "AVG_TEMPERATURE_AIR_2M_F", "MAX_TEMPERATURE_AIR_2M_F" AS "MAX_TEMPERATURE_AIR_2M_F", "MIN_TEMPERATURE_WETBULB_2M_F" AS "MIN_TEMPERATURE_WETBULB_2M_F", "AVG_TEMPERATURE_WETBULB_2M_F" AS "AVG_TEMPERATURE_WETBULB_2M_F", "MAX_TEMPERATURE_WETBULB_2M_F" AS "MAX_TEMPERATURE_WETBULB_2M_F", "MIN_TEMPERATURE_DEWPOINT_2M_F" AS "MIN_TEMPERATURE_DEWPOINT_2M_F", "AVG_TEMPERATURE_DEWPOINT_2M_F" AS "AVG_TEMPERATURE_DEWPOINT_2M_F", "MAX_TEMPERATURE_DEWPOINT_2M_F" AS "MAX_TEMPERATURE_DEWPOINT_2M_F", "MIN_TEMPERATURE_FEELSLIKE_2M_F" AS "MIN_TEMPERATURE_FEELSLIKE_2M_F", "AVG_TEMPERATURE_FEELSLIKE_2M_F" AS "AVG_TEMPERATURE_FEELSLIKE_2M_F", "MAX_TEMPERATURE_FEELSLIKE_2M_F" AS "MAX_TEMPERATURE_FEELSLIKE_2M_F", "MIN_TEMPERATURE_WINDCHILL_2M_F" AS "MIN_TEMPERATURE_WINDCHILL_2M_F", "AVG_TEMPERATURE_WINDCHILL_2M_F" AS "AVG_TEMPERATURE_WINDCHILL_2M_F", "MAX_TEMPERATURE_WINDCHILL_2M_F" AS "MAX_TEMPERATURE_WINDCHILL_2M_F", "MIN_TEMPERATURE_HEATINDEX_2M_F" AS "MIN_TEMPERATURE_HEATINDEX_2M_F", "AVG_TEMPERATURE_HEATINDEX_2M_F" AS "AVG_TEMPERATURE_HEATINDEX_2M_F", "MAX_TEMPERATURE_HEATINDEX_2M_F" AS "MAX_TEMPERATURE_HEATINDEX_2M_F", "MIN_HUMIDITY_RELATIVE_2M_PCT" AS "MIN_HUMIDITY_RELATIVE_2M_PCT", "AVG_HUMIDITY_RELATIVE_2M_PCT" AS "AVG_HUMIDITY_RELATIVE_2M_PCT", "MAX_HUMIDITY_RELATIVE_2M_PCT" AS "MAX_HUMIDITY_RELATIVE_2M_PCT", "MIN_HUMIDITY_SPECIFIC_2M_GPKG" AS "MIN_HUMIDITY_SPECIFIC_2M_GPKG", "AVG_HUMIDITY_SPECIFIC_2M_GPKG" AS "AVG_HUMIDITY_SPECIFIC_2M_GPKG", "MAX_HUMIDITY_SPECIFIC_2M_GPKG" AS "MAX_HUMIDITY_SPECIFIC_2M_GPKG", "MIN_PRESSURE_2M_MB" AS "MIN_PRESSURE_2M_MB", "AVG_PRESSURE_2M_MB" AS "AVG_PRESSURE_2M_MB", "MAX_PRESSURE_2M_MB" AS "MAX_PRESSURE_2M_MB", "MIN_PRESSURE_TENDENCY_2M_MB" AS "MIN_PRESSURE_TENDENCY_2M_MB", "AVG_PRESSURE_TENDENCY_2M_MB" AS "AVG_PRESSURE_TENDENCY_2M_MB", "MAX_PRESSURE_TENDENCY_2M_MB" AS "MAX_PRESSURE_TENDENCY_2M_MB", "MIN_PRESSURE_MEAN_SEA_LEVEL_MB" AS "MIN_PRESSURE_MEAN_SEA_LEVEL_MB", "AVG_PRESSURE_MEAN_SEA_LEVEL_MB" AS "AVG_PRESSURE_MEAN_SEA_LEVEL_MB", "MAX_PRESSURE_MEAN_SEA_LEVEL_MB" AS "MAX_PRESSURE_MEAN_SEA_LEVEL_MB", "MIN_WIND_SPEED_10M_MPH" AS "MIN_WIND_SPEED_10M_MPH", "AVG_WIND_SPEED_10M_MPH" AS "AVG_WIND_SPEED_10M_MPH", "MAX_WIND_SPEED_10M_MPH" AS "MAX_WIND_SPEED_10M_MPH", "AVG_WIND_DIRECTION_10M_DEG" AS "AVG_WIND_DIRECTION_10M_DEG", "MIN_WIND_SPEED_80M_MPH" AS "MIN_WIND_SPEED_80M_MPH", "AVG_WIND_SPEED_80M_MPH" AS "AVG_WIND_SPEED_80M_MPH", "MAX_WIND_SPEED_80M_MPH" AS "MAX_WIND_SPEED_80M_MPH", "AVG_WIND_DIRECTION_80M_DEG" AS "AVG_WIND_DIRECTION_80M_DEG", "MIN_WIND_SPEED_100M_MPH" AS "MIN_WIND_SPEED_100M_MPH", "AVG_WIND_SPEED_100M_MPH" AS "AVG_WIND_SPEED_100M_MPH", "MAX_WIND_SPEED_100M_MPH" AS "MAX_WIND_SPEED_100M_MPH", "AVG_WIND_DIRECTION_100M_DEG" AS "AVG_WIND_DIRECTION_100M_DEG", "TOT_PRECIPITATION_IN" AS "TOT_PRECIPITATION_IN", "TOT_SNOWFALL_IN" AS "TOT_SNOWFALL_IN", "TOT_SNOWDEPTH_IN" AS "TOT_SNOWDEPTH_IN", "MIN_CLOUD_COVER_TOT_PCT" AS "MIN_CLOUD_COVER_TOT_PCT", "AVG_CLOUD_COVER_TOT_PCT" AS "AVG_CLOUD_COVER_TOT_PCT", "MAX_CLOUD_COVER_TOT_PCT" AS "MAX_CLOUD_COVER_TOT_PCT", "MIN_RADIATION_SOLAR_TOTAL_WPM2" AS "MIN_RADIATION_SOLAR_TOTAL_WPM2", "AVG_RADIATION_SOLAR_TOTAL_WPM2" AS "AVG_RADIATION_SOLAR_TOTAL_WPM2", "MAX_RADIATION_SOLAR_TOTAL_WPM2" AS "MAX_RADIATION_SOLAR_TOTAL_WPM2", "TOT_RADIATION_SOLAR_TOTAL_WPM2" AS "TOT_RADIATION_SOLAR_TOTAL_WPM2" FROM FROSTBYTE_WEATHERSOURCE.ONPOINT_ID.HISTORY_DAY) AS SNOWPARK_LEFT INNER JOIN ( SELECT "POSTAL_CODE" AS "POSTAL_CODE_PC", "CITY_NAME" AS "CITY_NAME_PC", "COUNTRY" AS "COUNTRY_PC" FROM FROSTBYTE_WEATHERSOURCE.ONPOINT_ID.POSTAL_CODES) AS SNOWPARK_RIGHT ON (("POSTAL_CODE" = "POSTAL_CODE_PC") AND ("COUNTRY" = "COUNTRY_PC"))))) AS SNOWPARK_LEFT INNER JOIN ( SELECT "COUNTRY_ID" AS "COUNTRY_ID", "COUNTRY" AS "COUNTRY_C", "ISO_CURRENCY" AS "ISO_CURRENCY", "ISO_COUNTRY" AS "ISO_COUNTRY", "CITY_ID" AS "CITY_ID", "CITY" AS "CITY", "CITY_POPULATION" AS "CITY_POPULATION" FROM RAW_POS.COUNTRY) AS SNOWPARK_RIGHT ON (("COUNTRY" = "ISO_COUNTRY") AND ("CITY_NAME" = "CITY"))))) AS SNOWPARK_LEFT INNER JOIN ( SELECT "DATE" AS "DATE" FROM ( SELECT "DATE" FROM ( SELECT "ORDER_TS_DATE" AS "DATE" FROM HARMONIZED.ORDERS_STREAM) GROUP BY "DATE")) AS SNOWPARK_RIGHT ON ("DATE_VALID_STD" = "DATE"))) GROUP BY "DATE_VALID_STD", "CITY_NAME", "COUNTRY_C"))) AS SNOWPARK_RIGHT ON ((("DATE" = "DATE_W") AND ("CITY_NAME" = "CITY_NAME_W")) AND ("COUNTRY_DESC" = "COUNTRY_DESC_W"))))) ON ((("DATE" = "r_0003_DATE") AND ("CITY_NAME" = "r_0003_CITY_NAME")) AND ("COUNTRY_DESC" = "r_0003_COUNTRY_DESC")) WHEN  MATCHED  THEN  UPDATE  SET "DATE" = "r_0003_DATE", "CITY_NAME" = "r_0003_CITY_NAME", "COUNTRY_DESC" = "r_0003_COUNTRY_DESC", "DAILY_SALES" = "r_0003_DAILY_SALES", "AVG_TEMPERATURE_FAHRENHEIT" = "r_0003_AVG_TEMPERATURE_FAHRENHEIT", "AVG_TEMPERATURE_CELSIUS" = "r_0003_AVG_TEMPERATURE_CELSIUS", "AVG_PRECIPITATION_INCHES" = "r_0003_AVG_PRECIPITATION_INCHES", "AVG_PRECIPITATION_MILLIMETERS" = "r_0003_AVG_PRECIPITATION_MILLIMETERS", "MAX_WIND_SPEED_100M_MPH" = "r_0003_MAX_WIND_SPEED_100M_MPH", "META_UPDATED_AT" = current_timestamp() WHEN  NOT  MATCHED  THEN  INSERT ("DATE", "CITY_NAME", "COUNTRY_DESC", "DAILY_SALES", "AVG_TEMPERATURE_FAHRENHEIT", "AVG_TEMPERATURE_CELSIUS", "AVG_PRECIPITATION_INCHES", "AVG_PRECIPITATION_MILLIMETERS", "MAX_WIND_SPEED_100M_MPH", "META_UPDATED_AT") VALUES ("r_0003_DATE", "r_0003_CITY_NAME", "r_0003_COUNTRY_DESC", "r_0003_DAILY_SALES", "r_0003_AVG_TEMPERATURE_FAHRENHEIT", "r_0003_AVG_TEMPERATURE_CELSIUS", "r_0003_AVG_PRECIPITATION_INCHES", "r_0003_AVG_PRECIPITATION_MILLIMETERS", "r_0003_MAX_WIND_SPEED_100M_MPH", current_timestamp())
###