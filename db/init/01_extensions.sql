-- 只在資料磁碟區第一次初始化時執行（docker-entrypoint-initdb.d）
CREATE EXTENSION IF NOT EXISTS vector;
