from pyspark.sql import SparkSession
from pyspark.sql.functions import from_json, to_json, lit, col, unbase64, base64, split, expr
from pyspark.sql.types import StructField, StructType, StringType, FloatType, BooleanType, ArrayType, DateType
# create a StructType for the Kafka redis-server topic which has all changes made to Redis - before Spark 3.0.0, schema inference is not automatic
kafkaRedisServerSchema = StructType (
[
StructField("key", StringType()),
StructField("value", StringType()),
StructField("expiredType", StringType()),
StructField("expiredValue",StringType()),
StructField("existType", StringType()),
StructField("Ch", BooleanType()),
StructField("Incr", BooleanType()),
StructField("zSetEntries", ArrayType(
StructType([
StructField("element", StringType()),
StructField("Score", StringType())
]))
)
]
)
#create a StructType for the Customer JSON that comes from Redis- before Spark 3.0.0, schema inference is not automatic
kafkaCustomerJSON = StructType(
[
StructField("customerName", StringType()),
StructField("email", StringType()),
#StructField("score", StringType()),
StructField("birthDay", StringType())
]
)
# create a StructType for the Kafka stedi-events topic which has the Customer Risk JSON that comes from Redis- before Spark 3.0.0, schema inference is not automatic
riskJSONFromRedisSchema = StructType(
[
StructField("customer", StringType()),
StructField("score", FloatType()),
StructField("riskDate", DateType())
]
)
#create a spark application object
spark = SparkSession.builder.appName("sparkKafkaJoin").getOrCreate()
#set the spark log level to WARN
spark.sparkContext.setLogLevel('WARN')
# using the spark application object, read a streaming dataframe from the Kafka topic redis-server as the source
kafkaRedisServerDF = spark \
.readStream \
.format("kafka") \
.option("kafka.bootstrap.servers", "localhost:9092") \
.option("subscribe", "redis-server") \
.option("startingOffsets", "earliest") \
.load()
# Be sure to specify the option that reads all the events from the topic including those that were published before you started the spark stream
# cast the value column in the streaming dataframe as a STRING
kafkaRedisServerDF = kafkaRedisServerDF.selectExpr("cast(key as string) key","cast(value as string) value")
# parse the single column "value" with a json object in it, like this:
# +------------+
# | value |
# +------------+
# |{"key":"Q3..|
# +------------+
#
# with this JSON format: {"key":"Q3VzdG9tZXI=",
# "existType":"NONE",
# "Ch":false,
# "Incr":false,
# "zSetEntries":[{
# "element":"eyJjdXN0b21lck5hbWUiOiJTYW0gVGVzdCIsImVtYWlsIjoic2FtLnRlc3RAdGVzdC5jb20iLCJwaG9uZSI6IjgwMTU1NTEyMTIiLCJiaXJ0aERheSI6IjIwMDEtMDEtMDMifQ==",
# "Score":0.0
# }],
# "zsetEntries":[{
# "element":"eyJjdXN0b21lck5hbWUiOiJTYW0gVGVzdCIsImVtYWlsIjoic2FtLnRlc3RAdGVzdC5jb20iLCJwaG9uZSI6IjgwMTU1NTEyMTIiLCJiaXJ0aERheSI6IjIwMDEtMDEtMDMifQ==",
# "score":0.0
# }]
# }
#
# (Note: The Redis Source for Kafka has redundant fields zSetEntries and zsetentries, only one should be parsed)
#
# and create separated fields like this:
# +------------+-----+-----------+------------+---------+-----+-----+-----------------+
# | key|value|expiredType|expiredValue|existType| ch| incr| zSetEntries|
# +------------+-----+-----------+------------+---------+-----+-----+-----------------+
# |U29ydGVkU2V0| null| null| null| NONE|false|false|[[dGVzdDI=, 0.0]]|
# +------------+-----+-----------+------------+---------+-----+-----+-----------------+
#
# storing in a temporary view called RedisSortedSet
#
kafkaRedisServerDF.withColumn("value", from_json("value",kafkaRedisServerSchema ))\
.select(col('value.existType'), col('value.Ch'),\
col('value.Incr'), col('value.zSetEntries'))\
.createOrReplaceTempView("RedisSortedSet")
# execute a sql statement against a temporary view, which statement takes the element field from the 0th element in the array of structs and create a column called encodedCustomer
# the reason we do it this way is that the syntax available select against a view is different than a dataframe, and it makes it easy to select the nth element of an array in a sql column
zSetEntriesEncodedStreamingDF=spark.sql("select zSetEntries[0].element as encodedCustomer from RedisSortedSet")
# TO-DO: take the encodedCustomer column which is base64 encoded at first like this:
# +--------------------+
# | customer|
# +--------------------+
# |[7B 22 73 74 61 7...|
# +--------------------+
# and convert it to clear json like this:
# +--------------------+
# | customer|
# +--------------------+
# |{"customerName":"...|
#+--------------------+
#
# with this JSON format: {"customerName":"Sam Test","email":"sam.test@test.com","phone":"8015551212","birthDay":"2001-01-03"}
zSetDecodedEntriesStreamingDF=zSetEntriesEncodedStreamingDF\
.withColumn("customer", unbase64(zSetEntriesEncodedStreamingDF.encodedCustomer).cast("string"))
# parse the JSON in the Customer record and store in a temporary view called CustomerRecords
customerSchema = StructType (
[
StructField("customerName", StringType()),
StructField("email", StringType()),
#StructField("score", StringType()),
StructField("birthDay", StringType()),
]
)
zSetDecodedEntriesStreamingDF.withColumn("customer", from_json("customer", kafkaCustomerJSON))\
.select(col('customer.*')).createOrReplaceTempView("CustomerRecords")
# JSON parsing will set non-existent fields to null, so let's select just the fields we want, where they are not null as a new dataframe called emailAndBirthDayStreamingDF
emailAndBirthDayStreamingDF = spark.sql("select email,birthday as birthYear from CustomerRecords where email is not null and birthDay is not null")
# Split the birth year as a separate field from the birthday
emailAndBirthDayStreamingDF = emailAndBirthDayStreamingDF.withColumn('birthYear', split(emailAndBirthDayStreamingDF.birthYear,"-").getItem(0))
# Select only the birth year and email fields as a new streaming data frame called emailAndBirthYearStreamingDF
emailAndBirthYearStreamingDF = emailAndBirthDayStreamingDF.select(col('email'), col('birthYear'))
# using the spark application object, read a streaming dataframe from the Kafka topic stedi-events as the source
# Be sure to specify the option that reads all the events from the topic including those that were published before you started the spark stream
kafkaEventsFileDF = spark\
.readStream\
.format("kafka")\
.option("kafka.bootstrap.servers", "localhost:9092")\
.option("subscribe","stedi-events")\
.option("startingOffsets","earliest")\
.load()
# cast the value column in the streaming dataframe as a STRING
#value column
kafkaEventsFileDF = kafkaEventsFileDF.selectExpr("cast(value as string) value")
# TO-DO: parse the JSON from the single column "value" with a json object in it, like this:
# +------------+
# | value |
# +------------+
# |{"custom"...|
# +------------+
#
# and create separated fields like this:
# +------------+-----+-----------+
# | customer|score| riskDate |
# +------------+-----+-----------+
# |"sam@tes"...| -1.4| 2020-09...|
# +------------+-----+-----------+
#
# storing them in a temporary view called CustomerRisk
#.select(col('value.customer'), col('value.score'), col('value.riskDate'))
kafkaEventsFileDF.withColumn("value",from_json("value",riskJSONFromRedisSchema ))\
.select(col('value.customer'), col('value.score'), col('value.riskDate'))\
.createOrReplaceTempView("CustomerRisk")
# execute a sql statement against a temporary view, selecting the customer and the score from the temporary view, creating a dataframe called customerRiskStreamingDF
#selecting the customer and the score from the temporary view by using sql statement
customerRiskStreamingDF = spark.sql("select customer, score from CustomerRisk")
# join the streaming dataframes on the email address to get the risk score and the birth year in the same dataframe
# join the streaming dataframe by using joins
riskScoreStreamingDF = customerRiskStreamingDF.join(emailAndBirthYearStreamingDF, expr("customer = email"))
# sink the joined dataframes to a new kafka topic to send the data to the STEDI graph application
# +--------------------+-----+--------------------+---------+
# | customer|score| email|birthYear|
# +--------------------+-----+--------------------+---------+
# |Santosh.Phillips@...| -0.5|Santosh.Phillips@...| 1960|
# |Sean.Howard@test.com| -3.0|Sean.Howard@test.com| 1958|
# |Suresh.Clark@test...| -5.0|Suresh.Clark@test...| 1956|
# | Lyn.Davis@test.com| -4.0| Lyn.Davis@test.com| 1955|
# |Sarah.Lincoln@tes...| -2.0|Sarah.Lincoln@tes...| 1959|
# |Sarah.Clark@test.com| -4.0|Sarah.Clark@test.com| 1957|
# +--------------------+-----+--------------------+---------+
#
# In this JSON Format {"customer":"Santosh.Fibonnaci@test.com","score":"28.5","email":"Santosh.Fibonnaci@test.com","birthYear":"1963"}
#
query=riskScoreStreamingDF.selectExpr("To_JSON(struct(*)) AS value").writeStream\
.outputMode('append').format("kafka") \
.option("kafka.bootstrap.servers", "localhost:9092")\
.option("FailOnDataLoss" , "False").option("checkpointLocation","checkpoint")\
.option("topic", "risk-graph")\
.start();
query_runner=riskScoreStreamingDF.selectExpr("To_JSON(struct(*)) AS value").writeStream\
.outputMode('append')\
.format("console")\
.option('truncate' , False)\
.start()\

query.awaitTermination()

query_runner.awaitTermination()