-- 0007_conversation_hidden：使用者可從對話列表刪除（隱藏）自己的對話；資料本身不刪
-- 已套用的遷移檔不得修改；要改結構請新增下一號。遷移號 0007 由整合者於 2026-11-07 分配給 fix-followup-ui-20261107。
--
-- 2026-11-07：對話列表加刪除鍵；後端資料不刪。
-- 規範 1、8：請求紀錄、回饋、引用等要完整保留供管理日誌追溯，所以只記「使用者隱藏的時間」，
-- 不刪 conversations／requests 任何一列；隱藏後使用者端的列表、讀取、提問、改條件一律當作不存在（404）。

ALTER TABLE conversations ADD COLUMN hidden_at timestamptz;

COMMENT ON COLUMN conversations.hidden_at IS
  '使用者從對話列表刪除（隱藏）的時間；NULL＝未刪除。只隱藏，不刪資料（管理日誌仍可追溯）。';
