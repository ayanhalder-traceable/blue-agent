-- Tenant totals for the same day. Columns: spans, ips, users, apis, null_spans
SELECT count(*) spans, approx_distinct(ip_address) ips, approx_distinct(user_identifier) users,
  approx_distinct(api_name) apis, count_if(api_name IS NULL) null_spans
FROM span_event_view WHERE {{base}} AND {{day}}
