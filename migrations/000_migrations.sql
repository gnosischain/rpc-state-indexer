CREATE DATABASE IF NOT EXISTS {{database}};

CREATE TABLE IF NOT EXISTS {{database}}.migrations
(
    name String,
    checksum FixedString(64),
    applied_at DateTime64(9, 'UTC') DEFAULT now64(9)
)
ENGINE = MergeTree
PARTITION BY tuple()
ORDER BY name;
