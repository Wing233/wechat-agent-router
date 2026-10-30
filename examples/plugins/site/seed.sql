-- Demo data, all relative to now.
INSERT INTO visits(path, visitor_id, created_at) VALUES
  ('/', 'v1', datetime('now', '-2 days')),
  ('/', 'v2', datetime('now', '-2 days')),
  ('/blog', 'v1', datetime('now', '-1 day')),
  ('/', 'v3', datetime('now', '-1 day')),
  ('/docs', 'v2', datetime('now'));

INSERT INTO chat_messages(question, answer, created_at) VALUES
  ('这个站点是做什么的？', '一个自托管的个人站点。', datetime('now', '-1 day')),
  ('怎么联系站主？', '请通过站点联系页。', datetime('now'));
